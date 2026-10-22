---
name: synopsys-vcs
description: Compile and simulate SystemVerilog with Synopsys VCS, collect code, assertion and functional coverage, and report it with URG. For steps that build, run, check or measure RTL and testbenches with vcs, simv or urg.
tools: [vcs, urg, simv]
---

# Synopsys VCS: compile, simulate, coverage

Commands below were run in Super Partner's step sandbox. If the machine's own notes for VCS are
given too, they take precedence (environment script, licence, versions).

## Compile

```sh
vcs -full64 -sverilog -timescale=1ns/1ps \
    -cm line+cond+fsm+tgl+branch+assert -cm_dir cov/simv.vdb \
    -o simv rtl/*.sv tb/*.sv -l compile.log
```

- `-full64` and `-sverilog` are always needed for SystemVerilog.
- Coverage is decided at compile time: only metrics in `-cm` exist later. Put `assert` in it for
  SVA coverage; covergroups (functional coverage) are recorded without a `-cm` flag.
- One file list instead of many arguments: `-f files.f` (one path per line).
- Defines and parameters: `+define+NAME=VALUE`, `-pvalue+tb.DUT.WIDTH=8`.
- Read the end of `compile.log` for errors: `tail -40 compile.log`, `grep -n "Error-" compile.log`.
- Compile into a folder of its own per configuration (`-o build/cfg1/simv`), so runs do not clobber
  each other; `csrc/` and `simv.daidir/` are build output.

## Run

```sh
./simv -cm line+cond+fsm+tgl+branch+assert -cm_dir cov/simv.vdb -cm_name seed7 \
       +ntb_random_seed=7 -l run.log
```

- Repeat `-cm` at run time with the same metrics, and give each run its own `-cm_name` (the seed,
  the test name): runs into the same `.vdb` are merged.
- Random seed: `+ntb_random_seed=N` (read it in the testbench with
  `$value$plusargs("ntb_random_seed=%d", seed)` to print it).
- A test passes on what it prints, not on the exit code: end with an explicit line such as
  `TEST PASSED` / `TEST FAILED` and `grep` for it. `$fatal` makes simv exit non-zero.
- Assertion failures print `Assertion ... failed`; count them with `grep -c "failed at" run.log`.

## Coverage report (URG)

```sh
urg -full64 -dir cov/simv.vdb -report urgReport -format text -log urg.log
```

- Text reports land in `urgReport/`: `dashboard.txt` (total and per-metric scores),
  `groups.txt` (covergroups), `asserts.txt` (assertions), `hierarchy.txt`, `modinfo.txt`.
- Several databases: `-dir a.vdb -dir b.vdb`; HTML with `-format both`.
- `Warning-[UCAPI-SNF] Shape Not Found` for `Fsm` or `Condition` means the design has none of that
  kind; it is not an error. URG prints its copyright banner whatever `-log` says.

## Assertions and functional coverage

- Keep SVA in a checker module and attach it with `bind`, so the RTL stays clean:
  `bind async_fifo async_fifo_sva #(.W(W)) u_sva (.*);` compiled with the other files.
- Clock each property explicitly and reset it with `disable iff (!rst_n)`.
- For two clock domains, write each property in the domain whose signals it reads; check
  Gray-code pointers with `$onehot0(ptr ^ $past(ptr))` in their own domain.
- Covergroups: `covergroup cg @(posedge clk); ... endgroup` and `cg u_cg = new();` inside a
  module, or sample them from a class with `cg.sample()`.

## Errors seen in practice

- `Error-[USVSSIG] Unsupported SystemVerilog construct`: a variable declared with an initialiser
  inside a procedural block in a way VCS does not accept; declare it first, assign it after.
- Licence errors (`Unable to checkout`, `lmgrd`): not something a step can fix; say so.
- An `include` not found: add `+incdir+<dir>`.
