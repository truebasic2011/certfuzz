'''
This script looks for interesting crashes and rate them by potential exploitability
'''

import binascii
import logging
import os
import re
import struct

from certfuzz.tools.common.drillresults import ResultDriller
from certfuzz.tools.common.drillresults import TestCaseBundle
from certfuzz.tools.common.drillresults import carve
from certfuzz.tools.common.drillresults import carve2
from certfuzz.tools.common.drillresults import main as _main
from certfuzz.tools.common.drillresults import reg_set


logger = logging.getLogger(__name__)

regex = {
        '64bit_debugger': re.compile(r'^Microsoft.*AMD64$'),
        'bt_addr': re.compile(r'(0x[0-9a-fA-F]+)\s+.+$'),
        'current_instr': re.compile(r'^=>\s(0x[0-9a-fA-F]+)(.+)?:\s+(\S.+)'),
        'dbg_prompt': re.compile(r'^[0-9]:[0-9][0-9][0-9]> (.*)'),
        'frame0': re.compile(r'^#0\s+(0x[0-9a-fA-F]+)\s.+'),
        'gdb_report': re.compile(r'.+.gdb$'),
        'mapped_address': re.compile(r'^ModLoad: ([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+(.+)'),
        'mapped_address64': re.compile(r'^ModLoad: ([0-9a-fA-F]+`[0-9a-fA-F]+)\s+([0-9a-fA-F]+`[0-9a-fA-F]+)\s+(.+)'),
        'mapped_frame': re.compile(r'(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+0x[0-9a-fA-F]+\s+0(x0)?\s+(/.+)'),
        'regs1': re.compile(r'^eax=.+'),
        'regs2': re.compile(r'^eip=.+'),
        'syswow64': re.compile(r'ModLoad:.*syswow64.*', re.IGNORECASE),
        'vdso': re.compile(r'(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+0x[0-9a-fA-F]+\s+0(x0)?\s+\[vdso\]'),
        }


class LinuxTestCaseBundle(TestCaseBundle):
    really_exploitable = [
                  'SegFaultOnPc',
                  'BranchAv',
                  'StackCodeExection',
                  'BadInstruction',
                  'ReturnAv',
                  ]

    def _check_64bit(self):
        '''
        Check if the debugger and target app are 64-bit
        '''
        for line in self.reporttext.splitlines():
            m = re.match(regex['bt_addr'], line)
            if m:
                start_addr = m.group(1)
                if len(start_addr) > 10:
                    self._64bit_debugger = True
                    logger.debug()

    def _parse_testcase(self):
        '''
        Parse the gdb file
        '''
        crasherfile = self.testcase_file
        reporttext = self.reporttext
        _64bit_debugger = self._64bit_debugger
        crasherdata = self.crasherdata

    #    global _64bit_debugger

        # TODO move this back to ResultDriller class
#        if self.cached_results:
#            if self.cached_results.get(crash_hash):
#                self.results[crash_hash] = self.cached_results[crash_hash]
#                return

        details = self.details

        exceptionnum = 0
        classification = carve(reporttext, "Classification: ", "\n")
        #print 'classification: %s' % classification
        try:
            if classification:
                # Create a new exception dictionary to add to the crash
                exception = {}
                details['exceptions'][exceptionnum] = exception
        except KeyError:
            # Crash ID (crash_hash) not yet seen
            # Default it to not being "really exploitable"
            details['reallyexploitable'] = False
            # Create a dictionary of exceptions for the crash id
            exceptions = {}
            details['exceptions'] = exceptions
            # Create a dictionary for the exception
            details['exceptions'][exceptionnum] = exception

        # Set !exploitable classification for the exception
        if classification:
            details['exceptions'][exceptionnum]['classification'] = classification

        shortdesc = carve(reporttext, "Short description: ", " (")
        #print 'shortdesc: %s' % shortdesc
        if shortdesc:
            # Set !exploitable Short Description for the exception
            details['exceptions'][exceptionnum]['shortdesc'] = shortdesc
            # Flag the entire crash ID as really exploitable if this is a good
            # exception
            details['reallyexploitable'] = shortdesc in self.re_set

        if not os.path.isfile(crasherfile):
            # Can't find the crasher file
            #print "WTF! Cannot find %s" % crasherfile
            return
        # Set the "fuzzedfile" property for the crash ID
        details['fuzzedfile'] = crasherfile
        faultaddr = carve2(reporttext)
        instraddr = self.get_instr_addr()
        faultaddr = self.format_addr(faultaddr, _64bit_debugger)
        instraddr = self.format_addr(instraddr, _64bit_debugger)

        # No faulting address means no crash.
        if not faultaddr:
            return

        if instraddr:
            details['exceptions'][exceptionnum]['pcmodule'] = self.pc_in_mapped_address(instraddr)

        # Get the cdb line that contains the crashing instruction
        instructionline = self.get_instr(instraddr)
        details['exceptions'][exceptionnum]['instructionline'] = instructionline
        if instructionline:
            faultaddr = self.fix_efa_offset(instructionline, faultaddr)

        # Fix faulting pattern endian
        faultaddr = faultaddr.replace('0x', '')
        details['exceptions'][exceptionnum]['efa'] = faultaddr
        if _64bit_debugger:
            # 64-bit target app
            faultaddr = faultaddr.zfill(16)
            efaptr = struct.unpack('<Q', binascii.a2b_hex(faultaddr))
            efapattern = hex(efaptr[0]).replace('0x', '')
            efapattern = efapattern.replace('L', '')
            efapattern = efapattern.zfill(16)
        else:
            # 32-bit target app
            faultaddr = faultaddr.zfill(8)
            efaptr = struct.unpack('<L', binascii.a2b_hex(faultaddr))
            efapattern = hex(efaptr[0]).replace('0x', '')
            efapattern = efapattern.replace('L', '')
            efapattern = efapattern.zfill(8)

        # If there's a match, flag this exception has having Efa In File
        if binascii.a2b_hex(efapattern) in crasherdata:
            details['exceptions'][exceptionnum]['EIF'] = True
        else:
            details['exceptions'][exceptionnum]['EIF'] = False

    def format_addr(self, faultaddr):
        '''
        Format a 64- or 32-bit memory address to a fixed width
        '''

        if not faultaddr:
            return
        else:
            faultaddr = faultaddr.strip()
        faultaddr = faultaddr.replace('0x', '')

        if self._64bit_debugger:
            # Due to a bug in !exploitable, the Exception Faulting Address is
            # often wrong with 64-bit targets
            if len(faultaddr) < 10:
                # pad faultaddr
                faultaddr = faultaddr.zfill(16)
        else:
            if len(faultaddr) > 10:  # 0x12345678 = 10 chars
                faultaddr = faultaddr[-8:]
            elif len(faultaddr) < 10:
                # pad faultaddr
                faultaddr = faultaddr.zfill(8)

        return faultaddr

    def fix_efa_offset(self, instructionline, faultaddr):
        '''
        Adjust faulting address for instructions that use offsets
        Currently only works for instructions like CALL [reg + offset]
        '''
        if '0x' not in faultaddr:
            faultaddr = '0x' + faultaddr
        instructionpieces = instructionline.split()
        for index, piece in enumerate(instructionpieces):
            if piece == 'call':
                # CALL instruction
                if len(instructionpieces) <= index + 3:
                    # CALL to just a register.  No offset
                    return faultaddr
                address = instructionpieces[index + 3]
                if '+' in address:
                    splitaddress = address.split('+')
                    reg = splitaddress[0]
                    reg = reg.replace('[', '')
                    if reg not in reg_set:
                        return faultaddr
                    offset = splitaddress[1]
                    offset = offset.replace('h', '')
                    offset = offset.replace(']', '')
                    if '0x' not in offset:
                        offset = '0x' + offset
                    if int(offset, 16) > int(faultaddr, 16):
                        # TODO: fix up negative numbers
                        return faultaddr
                    # Subtract offset to get actual interesting pattern
                    faultaddr = hex(eval(faultaddr) - eval(offset))
                    faultaddr = self.format_addr(faultaddr.replace('L', ''))
        return faultaddr

    def get_instr(self, instraddr):
        '''
        Find the disassembly line for the current (crashing) instruction
        '''
        rgx = regex['current_instr']
        for line in self.reporttext.splitlines():
            n = rgx.match(line)
            if n:
                return n.group(3)
        return ''

    def fix_efa_bug(self, instraddr, faultaddr):
        '''
        !exploitable often reports an incorrect EFA for 64-bit targets.
        If we're dealing with a 64-bit target, we can second-guess the reported EFA
        '''
        instructionline = self.get_instr(instraddr)
        if not instructionline:
            return faultaddr
        ds = carve(instructionline, "ds:", "=")
        if ds:
            faultaddr = ds.replace('`', '')
        return faultaddr

    def pc_in_mapped_address(self, instraddr):
        '''
        Check if the instruction pointer is in a loaded module
        '''
        if not instraddr:
            # The gdb file doesn't have anything in it that'll tell us
            # where the PC is.
            return ''
    #    print 'checking if %s is mapped...' % instraddr
        mapped_module = 'unloaded'

        instraddr = int(instraddr, 16)
    #    print 'instraddr: %d' % instraddr
        for line in self.reporttext.splitlines():
            #print 'checking: %s for %s' % (line,regex['mapped_frame'])
            n = re.search(regex['mapped_frame'], line)
            if n:
    #            print '*** found mapped address regex!'
                # Strip out backticks present on 64-bit systems
                begin_address = int(n.group(1).replace('`', ''), 16)
                end_address = int(n.group(2).replace('`', ''), 16)
                if begin_address < instraddr < end_address:
                    mapped_module = n.group(4)
                    #print 'mapped_module: %s' % mapped_module
            else:
                # [vdso] still counts as a mapped module
                n = re.search(regex['vdso'], line)
                if n:
                    begin_address = int(n.group(1).replace('`', ''), 16)
                    end_address = int(n.group(2).replace('`', ''), 16)
                    if begin_address < instraddr < end_address:
                        mapped_module = '[vdso]'

        return mapped_module

    def get_ex_num(self):
        '''
        Get the exception number by counting the number of continues
        '''
        exception = 0
        for line in self.reporttext.splitlines():
            n = re.match(regex['dbg_prompt'], line)
            if n:
                cdbcmd = n.group(1)
                cmds = cdbcmd.split(';')
                for cmd in cmds:
                    if cmd == 'g':
                        exception = exception + 1
        return exception

    def get_instr_addr(self):
        '''
        Find the address for the current (crashing) instruction
        '''
        instraddr = None
        for line in self.reporttext.splitlines():
            #print 'checking: %s' % line
            n = re.match(regex['current_instr'], line)
            if n:
                instraddr = n.group(1)
                #print 'Found instruction address: %s' % instraddr
        if not instraddr:
            for line in self.reporttext.splitlines():
                #No disassembly. Resort to frame 0 address
                n = re.match(regex['frame0'], line)
                if n:
                    instraddr = n.group(1)
                    #print 'Found instruction address: %s' % instraddr
        return instraddr





class LinuxResultDriller(ResultDriller):
    def _platform_find_testcases(self, crash_hash, files, root):
                # Only use directories that are hashes
        # if "0x" in crash_hash:
            # Create dictionary for hashes in results dictionary
        crasherfile = ''
        # Check each of the files in the hash directory
        for current_file in files:
            # Go through all of the .gdb files and parse them
            if current_file.endswith('.gdb'):
#            if regex['gdb_report'].match(current_file):
                #print 'checking %s' % current_file
                dbg_file = os.path.join(root, current_file)
                logger.debug('found gdb file: %s', dbg_file)
                crasherfile = dbg_file.replace('.gdb', '')
                #crasherfile = os.path.join(root, crasherfile)
                tcb = LinuxTestCaseBundle(dbg_file, crasherfile, crash_hash,
                                          self.ignore_jit)
                self.testcase_bundles.append(tcb)


def main():
    _main(driller_class=LinuxResultDriller)

if __name__ == '__main__':
    main()
