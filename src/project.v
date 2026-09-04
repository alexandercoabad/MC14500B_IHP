`default_nettype none

module tt_um_mc14500b_soc_extended (
    input  wire [7:0] ui_in,    // Parallel Input Byte (Run Mode) / Instruction Byte (Prog Mode)
    output wire [7:0] uo_out,   // Parallel Latched Outputs
    input  wire [7:0] uio_in,   // Control Inputs: [7]=prog_mode, [6]=prog_we, [5:0]=prog_addr
    output wire [7:0] uio_out,  // Status Outputs: [5:0]=PC, [7]=Write Enable
    output wire [7:0] uio_oe,   // Dynamic IO Direction Control
    input  wire       ena,      // Tiny Tapeout Enable Pin
    input  wire       clk,      // System Clock
    input  wire       rst_n     // Active-Low Reset
);

    // Suppress unused signal warning for Verilator
    wire _unused_ok = &{1'b0, ena, 1'b0};

    // =========================================================================
    // 1. Core Memory and Control Registers
    // =========================================================================
    reg [7:0] prog_memory [0:63]; // 64-Byte Instruction Memory
    reg [7:0]  ram_bank;           // Registers 0-7: General RAM (scratch bits)
    reg [2:0]  ui_in_latch;        // One-cycle-delayed capture of ui_in[7:5],
                                    // exposed read-only at addresses 0xD/0xE/0xF.
                                    // (Only 3 bits are ever read back out of the
                                    // 8 that used to be captured here, so the
                                    // other 5 flip-flops were pure dead weight.)
    reg [5:0]  pc;                // Program Counter
    reg        instr_exec_done;   // Has the currently-fetched instruction already
                                   // fired its (one-shot) execution/write effects?

    // Program Execution / Configuration Signals
    wire prog_mode = uio_in[7];
    wire prog_we   = uio_in[6];
    wire [5:0] prog_addr = uio_in[5:0];

    // Instruction Decoding
    wire [7:0] current_instruction = prog_memory[pc];
    wire [3:0] opcode  = current_instruction[7:4];
    wire [3:0] operand = current_instruction[3:0];

    // Execution Core Signals
    wire core_data_in;
    wire core_write_en;
    wire core_data_out;
    reg  r_skip;

    reg r_rr, r_oen, r_ien;
    wire actual_data = core_data_in & r_ien;

    // The value STO/STOC actually intend to store: STOC (opcode 9) stores
    // the complement of RR, STO (opcode 8) stores RR as-is. This must be
    // used consistently by every write target (scratch RAM 0-7 *and* the
    // three peripheral registers below) so STOC behaves the same way
    // everywhere it's addressable.
    wire effective_write_data = (opcode == 4'h9) ? !core_data_out : core_data_out;

    // =========================================================================
    // 2. Feature 1: Ultra-Lightweight Edge Detector on ui_in[0] (Address 8)
    // =========================================================================
    reg ui_in0_d;
    reg edge_flag;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            ui_in0_d  <= 1'b0;
            edge_flag <= 1'b0;
        end else begin
            ui_in0_d <= ui_in[0];
            if (ui_in[0] && !ui_in0_d) begin
                edge_flag <= 1'b1; // Detect and latch rising edge (always live, independent of CPU stepping)
            end else if (!instr_exec_done && core_write_en && (operand == 4'h8) && effective_write_data) begin
                edge_flag <= 1'b0; // Clear flag on writing 1 to RAM address 8, once per instruction (not
                                    // re-gated by cpu_clk_step, so it fires exactly once even while the
                                    // clock divider parks the CPU on this instruction for many cycles)
            end
        end
    end

    // =========================================================================
    // 3. Feature 2: Compact Clock Divider (Address 9)
    // =========================================================================
    reg [11:0] slow_counter;
    reg        use_slow_clk;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            slow_counter <= 12'd0;
            use_slow_clk <= 1'b0;
        end else begin
            slow_counter <= slow_counter + 1'b1; // free-running; must NOT be gated by cpu_clk_step
            if (!instr_exec_done && core_write_en && (operand == 4'h9)) begin
                use_slow_clk <= effective_write_data; // Address 9 toggles slow execution, once per
                                                        // instruction, independent of cpu_clk_step
            end
        end
    end

    wire cpu_clk_step = use_slow_clk ? (slow_counter == 12'd0) : 1'b1;

    // =========================================================================
    // 4. Feature 3: Dedicated Output Latch Array (Address 12)
    // =========================================================================
    reg [7:0] latched_uo_out;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            latched_uo_out <= 8'h00;
        end else if (!instr_exec_done && core_write_en && (operand == 4'hC)) begin
            latched_uo_out <= {latched_uo_out[6:0], effective_write_data}; // Shift bit into Address 12, once
                                                                            // per instruction, independent of
                                                                            // cpu_clk_step (fires exactly once
                                                                            // even while parked by the divider)
        end
    end

    // =========================================================================
    // 5. Memory Bus Data Multiplexing
    // =========================================================================
    wire [15:0] mapped_ram_bank;
    assign mapped_ram_bank[7:0]   = ram_bank;
    assign mapped_ram_bank[8]     = edge_flag;          // Feature 1 readback
    assign mapped_ram_bank[9]     = use_slow_clk;       // Feature 2 readback
    assign mapped_ram_bank[11:10] = 2'b00;              // Reserved
    assign mapped_ram_bank[12]    = latched_uo_out[0];    // Feature 3 readback
    assign mapped_ram_bank[15:13] = ui_in_latch;

    assign core_data_in  = mapped_ram_bank[operand];
    assign core_data_out = r_rr;

    // Core Write Enable Output
    assign core_write_en = (!prog_mode) && (!r_skip) && r_oen && 
                           ((opcode == 4'h8) || (opcode == 4'h9));

    // =========================================================================
    // 6. Instruction RAM Dynamic Write Interface
    // =========================================================================
    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            for (i = 0; i < 64; i = i + 1) begin
                prog_memory[i] <= 8'h00;
            end
        end else if (prog_mode && prog_we) begin
            prog_memory[prog_addr] <= ui_in;
        end
    end

    // =========================================================================
    // 7. MC14500B Core Logic Execution Pipeline
    // =========================================================================
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pc              <= 6'b000000;
            ram_bank        <= 8'h00;
            ui_in_latch     <= 3'b000;
            r_rr            <= 1'b0;
            r_oen           <= 1'b1;
            r_ien           <= 1'b1;
            r_skip          <= 1'b0;
            instr_exec_done <= 1'b0;
        end else if (prog_mode) begin
            pc              <= 6'b000000;
            instr_exec_done <= 1'b0;
        end else begin
            // Execute the currently-fetched instruction's effects (ALU update,
            // r_skip/r_ien/r_oen updates, and scratch-RAM writes) exactly once,
            // on the first real clock edge after it becomes current. This is
            // intentionally decoupled from cpu_clk_step: gating execution on
            // cpu_clk_step would either re-run the same STO/STOC on every
            // physical edge while the clock divider parks the CPU (if
            // core_write_en weren't cpu_clk_step-qualified) or delay the write
            // until the divider's next pulse, up to ~4096 cycles later (if it
            // were). Instead we fire once immediately and then hold.
            if (!instr_exec_done) begin
                if (r_skip) begin
                    r_skip <= 1'b0;
                end else begin
                    case (opcode)
                        4'h0: ;                                // NOP0
                        4'h1: r_rr   <= actual_data;           // LD
                        4'h2: r_rr   <= !actual_data;          // LDC
                        4'h3: r_rr   <= r_rr & actual_data;    // AND
                        4'h4: r_rr   <= r_rr & (!actual_data); // ANDC
                        4'h5: r_rr   <= r_rr | actual_data;    // OR
                        4'h6: r_rr   <= r_rr | (!actual_data); // ORC
                        4'h7: r_rr   <= !(r_rr ^ actual_data); // XNOR
                        4'h8: ;                                // STO
                        4'h9: ;                                // STOC
                        4'hA: r_ien  <= actual_data;           // IEN
                        4'hB: r_oen  <= actual_data;           // OEN
                        4'hC: ;                                // JMP
                        4'hD: r_skip <= !r_rr;                 // RTN / SKZ
                        4'hE: ;                                // SKZ
                        4'hF: ;                                // NOPF
                    endcase
                end

                // Save bit logic to register addresses 0..7
                if (core_write_en && (operand < 4'h8)) begin
                    ram_bank[operand[2:0]] <= effective_write_data;
                end

                instr_exec_done <= 1'b1;
            end

            // Advance to the next instruction only as fast as the (possibly
            // divided-down) CPU clock step allows.
            if (cpu_clk_step) begin
                pc              <= pc + 1'b1;
                ui_in_latch     <= ui_in[7:5];
                instr_exec_done <= 1'b0;
            end
        end
    end

    // =========================================================================
    // 8. Output Signal Assignments
    // =========================================================================
    assign uo_out  = latched_uo_out;
    assign uio_out = prog_mode ? 8'h00 : {core_write_en, 1'b0, pc};
    assign uio_oe  = prog_mode ? 8'b00000000 : 8'b11111111;

endmodule
