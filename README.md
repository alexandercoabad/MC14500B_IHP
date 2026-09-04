![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# MC14500B Extended 1-bit Microcontroller SoC

An advanced, self-contained 1-bit Microcontroller System on Chip (SoC) centered around a hardened clone of the iconic 1977 Motorola MC14500B Industrial Control Unit (ICU). This layout occupies a **1x2 tile footprint** and is target-hardened specifically for the **TTIHP (IHP 130 nm BiCMOS SG13G2)** open-source silicon shuttle run.

Unlike a standalone CPU core, this macro design integrates a dynamic 64-byte program memory, static scratchpad registers, hardware-mapped peripherals (edge detector, clock divider, output latch array), and dedicated parallel I/O port interfaces directly into a single piece of silicon containing **2,697 standard cells** (excluding fill and decap cells).

---
## Layout

<img width="420" height="641" alt="Screenshot 2026-09-04 at 12 02 24 PM" src="https://github.com/user-attachments/assets/58a1a0f5-d33d-40aa-8e01-01c5333644f5" />

---

## Architecture Upgrades: Beyond the 1977 Motorola ICU

This design extends the classic 1-bit Motorola architecture into a fully autonomous microcontroller system:

1. **On-Chip Dynamic Program RAM (64 Bytes x 8-bit Width):** The original chip had no internal program storage. This SoC integrates 64 bytes of dynamically programmable instruction memory that can be loaded in Program Mode via the control inputs (`uio_in`).
2. **Integrated Program Counter (PC):** The standalone MC14500B lacked internal address indexing or a PC. This design includes an on-chip **6-bit hardware Program Counter register** that automatically increments on every valid execution step to loop through your instruction sequence.
3. **Internal Data Scratchpad RAM (8 Bits):** Features 8 addressable, single-bit static memory registers (`4'h0` to `4'h7`) allowing fast internal variable read/write operations.
4. **Memory-Mapped Integrated Peripherals:**
   * **Single-Bit Edge Detector (`4'h8`):** Hardware edge capture module that flags rising edge transitions on `ui_in[0]`. The flag is cleared by writing `1` to RAM address `8`.
   * **Programmable Clock Divider (`4'h9`):** Controls CPU execution speed rate. Writing `1` to address `9` enables slow execution mode.
   * **Dedicated Parallel Output Latch Array (`4'hC`):** Bit-addressable serial-in/parallel-out shift latch array driving hardware output port `uo_out[7:0]`.
5. **Real-Time Hardware Diagnostic Monitors (`uio_out[7:0]`):** When in Run Mode, the bidirectional status pins expose the 6-bit Program Counter (`PC`) and the core Write Enable flag directly to physical pins for logic analyzer probing and hardware debugging.

---

## Unified SoC Address Mapping Matrix

> **Note:** addresses `4'h8`-`4'hF` are **not** a uniform input port. Only three of
> those eight addresses (`4'hD`/`4'hE`/`4'hF`) tap the physical input pins, and each
> exposes exactly one bit — not the full `ui_in[7:0]` byte. `4'h8`, `4'h9`, and `4'hC`
> are dedicated peripheral registers, and `4'hA`-`4'hB` are hard-wired to `0`.

| Bit Address (Operand) | Target Subsystem | Operational Behavior |
| :--- | :--- | :--- |
| **`4'h0` to `4'h7`** | **Internal Scratchpad RAM** | General-purpose read/write single-bit data registers. |
| **`4'h8`** | **Rising Edge Detector** | Read edge flag status; write `1` to clear flag. |
| **`4'h9`** | **Clock Divider Control** | Read/write execution speed mode (1 = slow clock step, 0 = full speed). |
| **`4'hA` to `4'hB`** | **Hard-Wired Zero** | Always reads `0`; writes have no effect. |
| **`4'hC`** | **Output Latch Array** | Bitwise shift-in write access driving `uo_out[7:0]`. |
| **`4'hD`** | **Input Tap** | Read-only, one-cycle-delayed view of **`ui_in[5]`**. |
| **`4'hE`** | **Input Tap** | Read-only, one-cycle-delayed view of **`ui_in[6]`**. |
| **`4'hF`** | **Input Tap** | Read-only, one-cycle-delayed view of **`ui_in[7]`**. |

`ui_in[4:0]` are not directly addressable by the core at all; only the top three
input bits are latched and exposed as scratch-readable state.

For the full opcode-by-opcode reference (including a couple of places where the
RTL's own comments don't match its behavior), see **[ISA.md](ISA.md)**.

### Peripheral Write Timing Under the Clock Divider

Writes to the peripheral registers (`4'h8`, `4'h9`, `4'hC`, and general-purpose
`4'h0`-`4'h7`) fire **exactly once**, on the first real clock edge after the
instruction becomes current — independent of `cpu_clk_step`. This matters once the
clock divider (`4'h9`) parks the CPU on a `STO`/`STOC` instruction for many physical
clock cycles: the write commits immediately and then holds steady, rather than
either re-firing on every physical edge or being delayed until the divider's next
pulse.

---

## Automated Verification Workflows

The verification suite splits its pipeline tasks to guarantee absolute behavioral correctness and structural layout integrity before submission.

### 1. Behavioral RTL Simulation Loop
Driven locally or remotely by a Python-based `cocotb` test harness. 
* Navigate terminal focus into the verification folder: `cd test`
* Clean and fire up the simulation environment: `make clean && make`

The test framework configures a stable clock line, asserts a master reset sequence, injects binary vectors into the parallel inputs, loads instructions via dynamic programming mode, and validates output transitions.

The suite (`test/test.py`) currently runs **16 tests**, organized into three groups.
These run identically against both the RTL simulation and the post-synthesis
gate-level netlist (`gl_test` in CI).

**Group 1 — Core functional behavior** (must pass before any tapeout):

| Test | Verifies |
| :--- | :--- |
| `test_reset_state` | After reset, `pc = 0` and all outputs are zero. |
| `test_program_counter_increments_and_wraps` | `pc` free-runs through all 64 words and wraps `63 → 0`. |
| `test_program_mode_holds_pc_at_zero` | While `prog_mode = 1`, `pc` stays at `0` regardless of clock edges. |
| `test_ld_reads_external_input_with_one_cycle_latency` | `LD` from address `0xF` reflects `ui_in[7]`, latched one clock late. |
| `test_ldc_inverts_input` | `LDC` loads the complement of the addressed bit into `RR`. |
| `test_scratch_ram_write_and_readback` | Registers `0x0`-`0x7` are independently read/write-able. |
| `test_alu_truth_tables` | Directed truth-table coverage for `AND`/`ANDC`/`OR`/`ORC`/`XNOR`. |
| `test_ien_gates_data_input` | `IEN` (opcode `0xA`) gates subsequent addressed reads to `0` when cleared. |
| `test_oen_blocks_store` | `OEN` (opcode `0xB`) blocks `STO`/`STOC` writes when cleared. |
| `test_skip_opcode_skips_exactly_one_instruction` | The skip-if-zero behavior on opcode `0xD` suppresses exactly the next instruction. |
| `test_edge_detector_addr8` | A rising edge on `ui_in[0]` sets the edge flag, readable at `0x8`. |
| `test_output_latch_shifts_msb_first_history` | Repeated `STO 0xC` calls build up an 8-bit MSB-first shift history in `uo_out`. |
| `test_clock_divider_freezes_pc` | Enabling the clock divider (`STO 1` to address `0x9`) holds `pc` static for many cycles. |

**Group 2 — Regression tests for issues found during review** (each pins down a
specific fix so it can't silently regress):

| Test | Verifies |
| :--- | :--- |
| `test_bug_stoc_should_invert_for_peripheral_addresses` | `STOC` inverts `RR` the same way for peripheral addresses (`0x8`/`0x9`/`0xC`) as it does for scratch registers `0x0`-`0x7`. |
| `test_bug_peripheral_writes_alias_every_fastclock_while_parked` | Peripheral writes fire exactly once — not every physical clock cycle, and not delayed — while the clock divider parks the CPU on a `STO`/`STOC` instruction. |

**Group 3 — Documentation / spec-compliance checks** (guard against docs and RTL
silently drifting apart):

| Test | Verifies |
| :--- | :--- |
| `test_doc_addr8to15_are_not_a_uniform_input_port` | Address `0xA` is hard-wired to `0`, not a live `ui_in` tap, matching [ISA.md](ISA.md)'s address map rather than the old (incorrect) "uniform input port" description this test is named after. |

### 2. Gate-Level Netlist (GL) Layout Hardening
When OpenLane/LibreLane finishes layout compilation, a Gate-Level simulation (`GATES=yes`) verifies the synthesized netlist cells against the physical IHP standard cell simulation libraries.

* **Tooling Fix Note:** Because the IHP PDK simulation model files (`sg13g2_stdcell.v`) use advanced edge-sensitive timing rules wrapped inside `ifnone` constructs, standard open-source tools like Icarus Verilog v12 will crash. 
* To resolve this, the automated **`.github/workflows/gds.yaml`** configuration passes the argument **`IVVP_ARGS: "-gno-specify"`** directly into the testing container. This bypasses timing parameters, linking all standard cells together for a clean pass.

---

## Physical ASIC Configuration Properties
* **Process Technology Node:** IHP 130 nm BiCMOS (SG13G2)
* **Layout Footprint Allocation:** $1 \times 2$ Block
* **Total Logic Cell Count:** 2,662 Cells (excluding fill and decap cells)
* **Standard Cell Placement Utilization:** ~78.1%
* **Total Routing Wire Length:** 137,557 µm
* **Top-Level Interface Module Name:** `tt_um_mc14500b_soc_extended`
