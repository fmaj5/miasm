from __future__ import print_function
#
# Copyright (C) 2011 EADS France, Fabrice Desclaux <fabrice.desclaux@eads.net>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
from hashlib import md5
import warnings

from future.utils import viewvalues

from miasm.core.asmblock import disasmEngine, AsmBlockBad
from miasm.core.interval import interval
from miasm.core.utils import BoundedDict
from miasm.expression.expression import LocKey
from miasm.jitter.csts import *
from miasm.arch.arm.sem import *

class JitCore(object):

    "JiT management. This is an abstract class"

    # Jitted function's name
    FUNCNAME = "block_entry"

    jitted_block_delete_cb = None
    jitted_block_max_size = 10000

    def __init__(self, ir_arch, bin_stream):
        """Initialise a JitCore instance.
        @ir_arch: ir instance for current architecture
        @bin_stream: bin_stream instance
        """
        # Arch related
        self.ir_arch = ir_arch
        self.ircfg = self.ir_arch.new_ircfg()
        self.arch_name = "%s%s" % (self.ir_arch.arch.name, self.ir_arch.attrib)

        # Structures for block tracking
        self.offset_to_jitted_func = BoundedDict(self.jitted_block_max_size,
                                       delete_cb=self.jitted_block_delete_cb)
        self.loc_key_to_block = {}
        self.blocks_mem_interval = interval()

        # Logging & options
        self.log_mn = False
        self.log_regs = False
        self.log_newbloc = False
        self.options = {"jit_maxline": 50,  # Maximum number of line jitted
                        "max_exec_per_call": 0 # 0 means no limit
                        }

        # Disassembly Engine
        self.split_dis = set()
        self.mdis = disasmEngine(
            ir_arch.arch, ir_arch.attrib, bin_stream,
            lines_wd=self.options["jit_maxline"],
            loc_db=ir_arch.loc_db,
            follow_call=False,
            dontdis_retcall=False,
            split_dis=self.split_dis,
        )

        self.then_offsets = []
        self.else_offsets = []


    def set_options(self, **kwargs):
        "Set options relative to the backend"
        self.options.update(kwargs)

    def clear_jitted_blocks(self):
        "Reset all jitted blocks"
        self.offset_to_jitted_func.clear()
        self.loc_key_to_block.clear()
        self.blocks_mem_interval = interval()

    def add_disassembly_splits(self, *args):
        """The disassembly engine will stop on address in args if they
        are not at the block beginning"""
        self.split_dis.update(set(args))

    def remove_disassembly_splits(self, *args):
        """The disassembly engine will no longer stop on address in args"""
        self.split_dis.difference_update(set(args))

    def load(self):
        "Initialise the Jitter"
        raise NotImplementedError("Abstract class")

    def set_block_min_max(self, cur_block):
        "Update cur_block to set min/max address"

        if cur_block.lines:
            cur_block.ad_min = cur_block.lines[0].offset
            cur_block.ad_max = cur_block.lines[-1].offset + cur_block.lines[-1].l
        else:
            # 1 byte block for unknown mnemonic
            offset = ir_arch.loc_db.get_location_offset(cur_block.loc_key)
            cur_block.ad_min = offset
            cur_block.ad_max = offset+1


    def add_block_to_mem_interval(self, vm, block):
        "Update vm to include block addresses in its memory range"
        self.blocks_mem_interval += interval([(block.ad_min, block.ad_max - 1)])

        vm.reset_code_bloc_pool()
        for a, b in self.blocks_mem_interval:
            vm.add_code_bloc(a, b + 1)

    def add_block(self, block):
        """Add a block to JiT and JiT it.
        @block: asm_bloc to add
        """
        raise NotImplementedError("Abstract class")

    def disasm_and_jit_block(self, addr, vm):
        """Disassemble a new block and JiT it
        @addr: address of the block to disassemble (LocKey or int)
        @vm: VmMngr instance
        """

        # Get the block
        if isinstance(addr, LocKey):
            addr = self.ir_arch.loc_db.get_location_offset(addr)
            if addr is None:
                raise RuntimeError("Unknown offset for LocKey")

        # Prepare disassembler
        self.mdis.lines_wd = self.options["jit_maxline"]

        # Disassemble it
        cur_block = self.mdis.dis_block(addr)
        if isinstance(cur_block, AsmBlockBad):
            return cur_block
        # Logging
        if self.log_newbloc:
            print(cur_block.to_string(self.mdis.loc_db))

        # Update label -> block
        self.loc_key_to_block[cur_block.loc_key] = cur_block

        # Store min/max block address needed in jit automod code
        self.set_block_min_max(cur_block)

        # JiT it
        self.add_block(cur_block)

        # Update jitcode mem range
        self.add_block_to_mem_interval(vm, cur_block)
        return cur_block

    def parse_itt(self, instr):
        name = instr.name
        assert name.startswith('IT')
        name = name[1:]
        out = []
        for hint in name:
            if hint == 'T':
                out.append(0)
            elif hint == "E":
                out.append(1)
            else:
                raise ValueError("IT name invalid %s" % instr)
        return out, instr.args[0]

    def run_at(self, cpu, offset, stop_offsets):
        """Run from the starting address @offset.
        Execution will stop if:
        - max_exec_per_call option is reached
        - a new, yet unknown, block is reached after the execution of block at
          address @offset
        - an address in @stop_offsets is reached
        @cpu: JitCpu instance
        @offset: starting address (int)
        @stop_offsets: set of address on which the jitter must stop
        """

        if offset is None:
            offset = getattr(cpu, self.ir_arch.pc.name)

        if offset not in self.offset_to_jitted_func:
            # Need to JiT the block

            cur_block = self.mdis.dis_block(offset)
            if isinstance(cur_block, AsmBlockBad):
                errno = cur_block.errno
                if errno == AsmBlockBad.ERROR_IO:
                    cpu.vmmngr.set_exception(EXCEPT_ACCESS_VIOL)
                elif errno == AsmBlockBad.ERROR_CANNOT_DISASM:
                    cpu.set_exception(EXCEPT_UNK_MNEMO)
                else:
                    raise RuntimeError("Unhandled disasm result %r" % errno)
                return offset

            instr = cur_block.lines[0]
            if instr.name.startswith("IT"):

                print("it is IT block in jitter")

                assert len(self.then_offsets) == 0
                assert len(self.else_offsets) == 0

                self.then_offsets.clear()
                self.else_offsets.clear()

                print(instr)

                newpc = offset + instr.l

                it_hints, it_cond = self.parse_itt(instr)
                cond_num = cond_dct_inv[it_cond.name]
                cond_eq = tab_cond[cond_num]

                off = offset

                for hint in it_hints:

                    # get next inst offset
                    off = off + self.mdis.dis_block(off).lines[0].l

                    if hint == 0:
                        # then insts

                        if "EQ" in str(instr):
                            if cpu.zf == 1:
                                self.then_offsets.append([off, 1])
                            else:
                                self.then_offsets.append([off, 0])

                        elif "NE" in str(instr):
                            if cpu.zf == 0:
                                self.then_offsets.append([off, 1])
                            else:
                                self.then_offsets.append([off, 0])

                        elif "LT" in str(instr):
                            if cpu.of != cpu.nf:
                                self.then_offsets.append([off, 1])
                            else:
                                self.then_offsets.append([off, 0])

                        elif "CC" in str(instr):
                            if cpu.cf == 0:
                                self.then_offsets.append([off, 1])
                            else:
                                self.then_offsets.append([off, 0])

                        elif "LS" in str(instr):
                            if cpu.cf == 1 or cpu.zf == 0:
                                self.then_offsets.append([off, 1])
                            else:
                                self.then_offsets.append([off, 0])

                        elif "CS" in str(instr):
                            if cpu.cf == 1:
                                self.then_offsets.append([off, 1])
                            else:
                                self.then_offsets.append([off, 0])

                        else:
                            raise NotImplementedError("IT cond missing")

                    else:
                        # else insts

                        if "EQ" in str(instr):
                            if cpu.zf == 0:
                                self.else_offsets.append([off, 1])
                            else:
                                self.else_offsets.append([off, 0])

                        elif "NE" in str(instr):
                            if cpu.zf == 1:
                                self.else_offsets.append([off, 1])
                            else:
                                self.else_offsets.append([off, 0])

                        elif "LT" in str(instr):
                            if cpu.of != cpu.nf:
                                self.else_offsets.append([off, 0])
                            else:
                                self.else_offsets.append([off, 1])

                        elif "CC" in str(instr):
                            if cpu.cf == 0:
                                self.else_offsets.append([off, 0])
                            else:
                                self.else_offsets.append([off, 1])

                        elif "LS" in str(instr):
                            if cpu.cf == 1 or cpu.zf == 0:
                                self.else_offsets.append([off, 0])
                            else:
                                self.else_offsets.append([off, 1])

                        elif "CS" in str(instr):
                            if cpu.cf == 1:
                                self.else_offsets.append([off, 0])
                            else:
                                self.else_offsets.append([off, 1])


                        else:
                            raise NotImplementedError("IT cond missing")

                return newpc

            for off, ex in self.then_offsets:
                if off == offset:

                    print("jit: offset {0:x} behind IT block".format(offset))

                    of, ex = self.then_offsets.pop(0)
                    assert of == offset

                    # ignore and inc pc
                    if ex == 0:
                        print("cond unstatify, ignore")
                        return offset + instr.l
                    else:
                        print("cond statify, jit")
                        break

            for off, ex in self.else_offsets:
                if off == offset:

                    print("jit: offset {0:x} behind IT block".format(offset))
                    of, ex = self.else_offsets.pop(0)
                    assert of == offset

                    if ex == 0:
                        print("cond unstatify, ignore")
                        return offset + instr.l
                    else:
                        print("cond statify, jit")
                        break

            cur_block = self.disasm_and_jit_block(offset, cpu.vmmngr)
            if isinstance(cur_block, AsmBlockBad):
                errno = cur_block.errno
                if errno == AsmBlockBad.ERROR_IO:
                    cpu.vmmngr.set_exception(EXCEPT_ACCESS_VIOL)
                elif errno == AsmBlockBad.ERROR_CANNOT_DISASM:
                    cpu.set_exception(EXCEPT_UNK_MNEMO)
                else:
                    raise RuntimeError("Unhandled disasm result %r" % errno)
                return offset

        # Run the block and update cpu/vmmngr state
        return self.exec_wrapper(offset, cpu, self.offset_to_jitted_func.data,
                                 stop_offsets,
                                 self.options["max_exec_per_call"])

    def blocks_to_memrange(self, blocks):
        """Return an interval instance standing for blocks addresses
        @blocks: list of AsmBlock instances
        """

        mem_range = interval()

        for block in blocks:
            mem_range += interval([(block.ad_min, block.ad_max - 1)])

        return mem_range

    def __updt_jitcode_mem_range(self, vm):
        """Rebuild the VM blocks address memory range
        @vm: VmMngr instance
        """

        # Reset the current pool
        vm.reset_code_bloc_pool()

        # Add blocks in the pool
        for start, stop in self.blocks_mem_interval:
            vm.add_code_bloc(start, stop + 1)

    def del_block_in_range(self, ad1, ad2):
        """Find and remove jitted block in range [ad1, ad2].
        Return the list of block removed.
        @ad1: First address
        @ad2: Last address
        """

        # Find concerned blocks
        modified_blocks = set()
        for block in viewvalues(self.loc_key_to_block):
            if not block.lines:
                continue
            if block.ad_max <= ad1 or block.ad_min >= ad2:
                # Block not modified
                pass
            else:
                # Modified blocks
                modified_blocks.add(block)

        # Generate interval to delete
        del_interval = self.blocks_to_memrange(modified_blocks)

        # Remove interval from monitored interval list
        self.blocks_mem_interval -= del_interval

        # Remove modified blocks
        for block in modified_blocks:
            try:
                for irblock in block.blocks:
                    # Remove offset -> jitted block link
                    offset = self.ir_arch.loc_db.get_location_offset(irblock.loc_key)
                    if offset in self.offset_to_jitted_func:
                        del(self.offset_to_jitted_func[offset])

            except AttributeError:
                # The block has never been translated in IR
                offset = self.ir_arch.loc_db.get_location_offset(block.loc_key)
                if offset in self.offset_to_jitted_func:
                    del(self.offset_to_jitted_func[offset])

            # Remove label -> block link
            del(self.loc_key_to_block[block.loc_key])

        return modified_blocks

    def updt_automod_code_range(self, vm, mem_range):
        """Remove jitted code in range @mem_range
        @vm: VmMngr instance
        @mem_range: list of start/stop addresses
        """
        for addr_start, addr_stop in mem_range:
            self.del_block_in_range(addr_start, addr_stop)
        self.__updt_jitcode_mem_range(vm)
        vm.reset_memory_access()

    def updt_automod_code(self, vm):
        """Remove jitted code updated by memory write
        @vm: VmMngr instance
        """
        mem_range = []
        for addr_start, addr_stop in vm.get_memory_write():
            mem_range.append((addr_start, addr_stop))
        self.updt_automod_code_range(vm, mem_range)

    def hash_block(self, block):
        """
        Build a hash of the block @block
        @block: asmblock
        """
        block_raw = b"".join(line.b for line in block.lines)
        offset = self.ir_arch.loc_db.get_location_offset(block.loc_key)
        block_hash = md5(
            b"%X_%s_%s_%s_%s" % (
                offset,
                self.arch_name.encode(),
                b'\x01' if self.log_mn else b'\x00',
                b'\x01' if self.log_regs else b'\x00',
                block_raw
            )
        ).hexdigest()
        return block_hash

    @property
    def disasm_cb(self):
        warnings.warn("Deprecated API: use .mdis.dis_block_callback")
        return self.mdis.dis_block_callback

    @disasm_cb.setter
    def disasm_cb(self, value):
        warnings.warn("Deprecated API: use .mdis.dis_block_callback")
        self.mdis.dis_block_callback = value
