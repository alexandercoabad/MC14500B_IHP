# MC14500B Extended SoC — Instruction Set Architecture Reference

This document describes the instruction set as actually implemented in
[`src/project.v`](src/project.v) for the `tt_um_mc14500b_soc_extended` core. It is
meant to be the single source of truth for opcode behavior — where the RTL's own
comments are misleading (a couple of them are, see below), this document follows
the RTL's *behavior*, not its comments.

---

## 1. Instruction Word Format

Each instruction is one byte, fetched from the 64-word program memory at the
current Program Counter (`PC`) value:

```
 7   6   5   4   3   2   1   0
+---+---+---+---+---+---+---+---+
|      opcode       |  operand  |
+---+---+---+---+---+---+---+---+
```

* **`opcode` (`[7:4]`)** — selects one of 16 operations.
* **`operand` (`[3:0]`)** — a 4-bit address into the unified memory map (see
  [§4](#4-operand-address-map)) that most opcodes read from and/or write to.

The Program Counter is 6 bits wide (0–63) and free-runs, wrapping from 63 back to
0. It does **not** support any form of relative or absolute jump — see the note on
opcode `0xC` below.

---

## 2. Registers

| Register | Width | Visibility | Description |
| :--- | :---: | :--- | :--- |
| **`RR`** (Result Register) | 1 bit | Internal | The core's single-bit accumulator. Every ALU opcode reads and/or writes it. |
| **`IEN`** (Input Enable) | 1 bit | Internal, resets to `1` | When `0`, forces every addressed *read* to `0` (see [§3](#3-data-path)). Set/cleared by opcode `0xA`. |
| **`OEN`** (Output Enable) | 1 bit | Internal, resets to `1` | When `0`, suppresses `STO`/`STOC` writes entirely. Set/cleared by opcode `0xB`. |
| **`PC`** (Program Counter) | 6 bits | `uio_out[5:0]` | Address of the currently-fetched instruction. |
| *(internal skip latch)* | 1 bit | Not exposed | Set by opcode `0xD` when `RR == 0`; causes the *next* instruction to execute as a no-op. |

`uio_out[7]` also exposes the core's write-enable strobe (high exactly on the cycle
a `STO`/`STOC` write commits); `uio_out[6]` is unused and always reads `0`.

---

## 3. Data Path

Every opcode that reads memory reads through the same gated data path:

```
addressed_bit = mapped_ram_bank[operand]   // see §4 for what this resolves to
actual_data   = addressed_bit & IEN
```

**`IEN` gates the raw bit *before* any opcode-level complementing.** For example,
if `IEN = 0`, `LDC` does not "load the complement of the addressed bit" — it loads
the complement of `0`, i.e. `RR` is forced to `1`, regardless of what the addressed
bit actually is. The same applies to `ANDC`/`ORC`. This is a direct consequence of
gating happening on `actual_data`, not on the raw memory bit.

Every opcode that writes memory writes through a similarly shared path:

```
effective_write_data = (opcode == STOC) ? !RR : RR
```

`STO` always writes `RR` as-is; `STOC` always writes its complement. This is
applied uniformly to the scratchpad registers (`0x0`-`0x7`) *and* the three
writable peripheral registers (`0x8`, `0x9`, `0xC`) — `STOC` inverts the same way
everywhere it's addressable. A write only actually commits when **all** of the
following hold:

* `OEN = 1`,
* the instruction is not currently being skipped, and
* the opcode is `STO` (`0x8`) or `STOC` (`0x9`).

### Execution timing

Each fetched instruction executes its effects (ALU update, register writes,
`IEN`/`OEN` updates, scratch/peripheral writes) **exactly once**, on the first real
clock edge after it becomes current — independent of the clock divider
(operand `0x9`, [§4](#4-operand-address-map)). If the divider has parked the CPU on
an instruction for many physical clock cycles, the write still commits immediately
and then holds steady; it does not re-fire every cycle, nor does it wait for the
divider's next pulse.

---

## 4. Operand Address Map

The 4-bit `operand` field addresses a *unified* 16-entry memory space. It is **not**
a uniform block of general RAM — only `0x0`–`0x7` are read/write scratch bits.
Everything from `0x8` up is either a special-purpose peripheral register, hard-wired
to `0`, or a read-only input tap:

| Address | Target | Read returns... | Write does... |
| :--- | :--- | :--- | :--- |
| `0x0`–`0x7` | General-purpose scratchpad RAM | The stored bit | Stores `effective_write_data` into that bit |
| `0x8` | Rising-edge detector | The latched edge flag (set on any rising edge of `ui_in[0]`, live and independent of CPU stepping) | Writing a logical `1` clears the flag; writing `0` has no effect |
| `0x9` | Clock divider control | Current divider state (`1` = slow, `0` = full speed) | `1` parks instruction stepping to roughly once every 4096 clock cycles; `0` returns to full speed |
| `0xA`–`0xB` | Hard-wired zero | Always `0` | No effect |
| `0xC` | Output latch array | Bit `0` of the 8-bit output shift register | Shifts `effective_write_data` into the register that drives `uo_out[7:0]` (MSB-first history) |
| `0xD` | Input tap | One-cycle-delayed `ui_in[5]` | No effect (read-only) |
| `0xE` | Input tap | One-cycle-delayed `ui_in[6]` | No effect (read-only) |
| `0xF` | Input tap | One-cycle-delayed `ui_in[7]` | No effect (read-only) |

`ui_in[4:0]` are **not** addressable by the core at all — only the top 3 input bits
are ever captured.

---

## 5. Opcode Reference

| Hex | RTL Behavior | Description |
| :---: | :--- | :--- |
| **`0x0`** | *(no operation)* | **NOP0.** `RR` and all other state unchanged. |
| **`0x1`** | `RR <= actual_data` | **LD.** Load the addressed bit (gated by `IEN`) into `RR`. |
| **`0x2`** | `RR <= !actual_data` | **LDC.** Load the complement of the addressed bit into `RR`. See the `IEN`-gating note in [§3](#3-data-path). |
| **`0x3`** | `RR <= RR & actual_data` | **AND.** |
| **`0x4`** | `RR <= RR & !actual_data` | **ANDC.** AND with the complement of the addressed bit. |
| **`0x5`** | `RR <= RR \| actual_data` | **OR.** |
| **`0x6`** | `RR <= RR \| !actual_data` | **ORC.** OR with the complement of the addressed bit. |
| **`0x7`** | `RR <= !(RR ^ actual_data)` | **XNOR.** |
| **`0x8`** | Writes `RR` to the addressed location (subject to §3's write conditions) | **STO.** Store `RR` as-is. |
| **`0x9`** | Writes `!RR` to the addressed location (subject to §3's write conditions) | **STOC.** Store the complement of `RR`. |
| **`0xA`** | `IEN <= actual_data` | **IEN.** Load the addressed bit into the Input Enable register. The addressed bit is itself read through the *current* (pre-update) `IEN` value. |
| **`0xB`** | `OEN <= actual_data` | **OEN.** Load the addressed bit into the Output Enable register. |
| **`0xC`** | *(no operation)* | Decoded but implemented as a no-op. **Note:** the RTL comment on this line says `// JMP`, but no jump, flag pulse, or any other side effect actually occurs — `PC` simply continues incrementing normally. (On the original 1977 MC14500B, `JMP` never performed an internal jump either — the chip has no address register, and `JMP` only pulsed an external `Flag O` pin for surrounding hardware to act on. This SoC does not implement that external flag pulse, so the opcode is a true no-op here, not just "no internal jump.") |
| **`0xD`** | `skip <= !RR` | Sets the internal skip latch when `RR == 0`; the **next** fetched instruction then executes as a no-op (see [§6](#6-skip-behavior)). **Note:** the RTL comment on this line says `// RTN / SKZ`. The implemented behavior matches "Skip if Zero," not "Return" — there is no return-address mechanism anywhere in this design. |
| **`0xE`** | *(no operation)* | **Note:** the RTL comment on this line says `// SKZ`, but this opcode is a plain no-op — the actual skip-if-zero behavior lives on `0xD`, not here. Treat this comment as a leftover/typo, not a spec. |
| **`0xF`** | *(no operation)* | **NOPF.** |

---

## 6. Skip Behavior

Opcode `0xD` is the only opcode that affects control flow, and it does so without
any address register: when `RR == 0` at the time `0xD` executes, an internal
1-cycle skip latch is set. On the following instruction:

* the skip latch is cleared (consuming the skip), and
* the fetched instruction's opcode is **not** decoded/executed at all — no `RR`
  update, no `IEN`/`OEN` update, and (critically) `core_write_en` is forced low, so
  even a skipped `STO`/`STOC` does not write anything.

`PC` still advances normally through a skipped instruction; skipping suppresses an
instruction's *effects*, not its fetch/advance.

---

## 7. Worked Example

```text
Address  Instruction    Effect
-------  -----------    ------
  0x00   LDC  0x0       RR <= !reg[0]        ; reg[0] is 0 post-reset, so RR becomes 1
  0x01   STO  0x9       reg[9] <= RR         ; enables the clock divider (parks the CPU)
  0x02   STO  0xC       shift RR into uo_out ; fires once immediately, then holds
  0x03   NOP0
   ...   (63 more NOP0s, PC wraps back to 0x00 once the divider allows it)
```

After this sequence, `uo_out` settles to `0x01` almost immediately and then holds
steady for roughly 4096 clock cycles per step while the divider is enabled — see
[§3](#3-data-path)'s execution-timing note.

---

## See Also

* [`README.md`](README.md) — project overview, physical implementation stats, and
  the peripheral address map summary.
* [`info.md`](info.md) — Tiny Tapeout datasheet source.
* [`test/test.py`](test/test.py) — the `cocotb` test suite; several tests
  (`test_bug_*`, `test_doc_*`) exist specifically to pin down behavior that is easy
  to get wrong when reading the RTL comments alone.
