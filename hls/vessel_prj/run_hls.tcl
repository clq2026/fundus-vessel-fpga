# =============================================================================
# Vitis HLS 2022.2 工程创建/仿真脚本（眼底血管中值+黑帽 IP）
# 目标器件：PYNQ-Z2 = xc7z020clg400-1
#
# 用法（在本文件所在目录 vessel_prj 下打开 Vitis HLS 命令行）：
#   vitis_hls -f run_hls.tcl            # 建工程 + C仿真(C Sim)
#   vitis_hls -f run_hls.tcl cosim      # 再做 C/RTL 协同仿真（默认不记波形，快）
#   vitis_hls -f run_hls.tcl cosim trace # 协同仿真 + 记录完整波形（慢、占磁盘，排错用）
#   vitis_hls -f run_hls.tcl synth      # 再做综合并导出 IP
#
# 注意：本脚本用 open_project -reset / open_solution -reset，
#       每次运行都会清空 vessel_prj/ 与 solution1/（含已导出的 export.zip）。
#       跑 cosim 前请先备份 export.zip，跑完再执行 synth 重新导出。
# =============================================================================

set PROJECT   "vessel_prj"
set SOLUTION  "solution1"
set PART      "xc7z020clg400-1"
set CLOCK_NS  10
set TOP       "vessel_top"

# 让 testbench 能找到测试向量：用绝对路径，避免 C 仿真时工作目录变化
# info script 指向本 tcl 文件，test_vectors 在本文件的上一级目录
set SCRIPT_DIR [file normalize [file dirname [info script]]]
set ::env(TV_DIR) [file normalize "$SCRIPT_DIR/../test_vectors"]

open_project -reset $PROJECT
    add_files "vessel_top.cpp"
    add_files -tb "tb_vessel.cpp"
    set_top $TOP

    open_solution -reset $SOLUTION
        set_part $PART
        create_clock -period $CLOCK_NS -name default
        # 2022.2 默认 Vitis HLS 流程
        csim_design -O -clean

        # 注意：cosim_design 必须先有 RTL 综合结果，否则报
        #   ERROR: [COSIM 212-40] ... The solution has been reset
        # 因此 cosim 流程里先跑一次 csynth_design。
        set DID_SYNTH 0

        if {[lsearch $argv "cosim"] >= 0} {
            # 协同仿真波形记录级别：
            #   none = 不记波形（默认，快、省磁盘，适合首次功能验证）
            #   all  = 记录全部内部信号（慢、波形文件可能数 GB，排错时才用）
            set TRACE_LEVEL "none"
            if {[lsearch $argv "trace"] >= 0} { set TRACE_LEVEL "all" }
            csynth_design
            set DID_SYNTH 1
            cosim_design -O -trace_level $TRACE_LEVEL
        }
        if {[lsearch $argv "synth"] >= 0} {
            if {!$DID_SYNTH} { csynth_design }
            export_design -format ip_catalog
        }
    close_solution
close_project

# 末尾必须 exit：否则工具会进入交互模式去读 stdin，在 nohup 后台运行时
# 报 error reading "stdin": bad file number -> ERROR: [HLS 200-70]（假报错，退出码 1）
exit
