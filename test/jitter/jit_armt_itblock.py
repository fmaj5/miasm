import sys
from miasm.core.utils import decode_hex
from miasm.jitter.csts import PAGE_READ, PAGE_WRITE, EXCEPT_UNK_MNEMO
from miasm.analysis.machine import Machine
from miasm.analysis.dse import DSEEngine

machine = Machine('armtl')

jitter = machine.jitter(sys.argv[1])

jitter.init_stack()

data = decode_hex("002818bf0120012001202ce0")

run_addr = 0x40000000

jitter.vm.add_memory_page(run_addr, PAGE_READ | PAGE_WRITE, data)

jitter.set_trace_log()

dse = DSEEngine(machine)
dse.attach(jitter)

jitter.cpu.R0 = 1
# jitter.cpu.R0 = 0

dse.update_state_from_concrete()

jitter.init_run(run_addr)
jitter.continue_run()
