# =============================================================================
#  S10 阶段二：生成 HDL Wrapper + 综合（synth_1）+ 只读报告
#
#  用法（在 Linux 终端）：
#     vivado -mode batch -nojournal -nolog -source ~/s10/build_synth.tcl
#
#  本脚本只做【综合】，不做实现、不出 bitstream。
#  目的：在花 20-40 分钟跑实现之前，先把三件事确认掉：
#     1) 综合有没有报错；
#     2) FCLK_CLK0 的时钟约束周期是不是 10.000 ns（100MHz）——验证 PS 配置无误；
#     3) 血管 IP 在真实综合网表里的 WNS（HLS 只给过 +0.032ns 的估算值）。
#
#  报告文件写到 ~/s10/report/
# =============================================================================

set HOME_DIR  $::env(HOME)
set PROJ_DIR  "$HOME_DIR/projects/vivado/vessel_sys"
set PROJ_NAME "vessel_sys"
set BD_NAME   "vessel_sys"
set RPT_DIR   "$HOME_DIR/s10/report"

file mkdir $RPT_DIR


# ---------------------------------------------------------------------------
puts "===== S-01 打开工程 ====="
open_project "$PROJ_DIR/$PROJ_NAME.xpr"
puts "  工程 : [get_property DIRECTORY [current_project]]"
puts "  器件 : [get_property part [current_project]]"
puts "  板卡 : [get_property board_part [current_project]]"


# ---------------------------------------------------------------------------
puts "===== S-02 生成 HDL Wrapper 并设为顶层 ====="
set bdf [get_files -quiet "$BD_NAME.bd"]
if {[llength $bdf] == 0} { error "S-02-FAIL: 工程里找不到 $BD_NAME.bd" }

if {[catch { set wl [make_wrapper -files [lindex $bdf 0] -top] } err]} {
    puts "!! make_wrapper 失败: $err"
    exit 1
}
puts "  wrapper 生成结果: $wl"

foreach w $wl {
    if {[llength [get_files -quiet $w]] == 0} {
        add_files -norecurse $w
        puts "  已加入工程: $w"
    } else {
        puts "  已在工程中: $w"
    }
}
update_compile_order -fileset sources_1
set_property top ${BD_NAME}_wrapper [current_fileset]
update_compile_order -fileset sources_1
puts "  顶层模块: [get_property top [current_fileset]]"


# ---------------------------------------------------------------------------
puts "===== S-03 启动综合（synth_1），不要关终端 ====="
reset_run synth_1
if {[catch { launch_runs synth_1 -jobs 4 } err]} { puts "!! launch_runs: $err" }
wait_on_run synth_1
set st [get_property STATUS [get_runs synth_1]]
set pr [get_property PROGRESS [get_runs synth_1]]
puts "  synth_1 状态 : $st"
puts "  synth_1 进度 : $pr"

if {![string match "*Complete*" $st]} {
    puts "!! 综合未正常完成，状态=$st —— 不再继续"
    exit 1
}


# ---------------------------------------------------------------------------
puts "===== S-04 打开综合结果 ====="
if {[catch { open_run synth_1 } err]} {
    puts "!! open_run synth_1 失败: $err"
    exit 1
}
puts "  已打开 synth_1"

puts "--- 4.1 时钟约束（核对 FCLK_CLK0 = 10.000 ns）---"
report_clocks -file "$RPT_DIR/clocks.rpt"
foreach c [get_clocks -quiet] {
    set p "?"
    catch { set p [get_property PERIOD $c] }
    puts [format "   %-44s 周期 = %s ns" $c $p]
}

puts "--- 4.2 顶层端口（应含 DDR / FIXED_IO）---"
foreach pt [get_ports -quiet] { puts "     $pt" }

puts "--- 4.3 时序（综合估算，非最终值）---"
report_timing_summary -file "$RPT_DIR/timing_synth.rpt" -delay_type max
set wp [get_timing_paths -delay_type max -max_paths 1 -quiet]
set wns "?"
set wns_src "?"
set wns_dst "?"
if {[llength $wp] > 0} {
    set p0 [lindex $wp 0]
    catch { set wns [get_property SLACK $p0] }
    catch { set wns_src [get_property STARTPOINT_PIN $p0] }
    catch { set wns_dst [get_property ENDPOINT_PIN $p0] }
}
puts "  建立(setup) 最差裕量 WNS = $wns ns"
puts "     起点: $wns_src"
puts "     终点: $wns_dst"

set wph [get_timing_paths -delay_type min -max_paths 1 -quiet]
set whs "?"
if {[llength $wph] > 0 } { catch { set whs [get_property SLACK [lindex $wph 0]] } }
puts "  保持(hold) 最差裕量 WHS = $whs ns"

puts "--- 4.4 资源占用 ---"
report_utilization -file "$RPT_DIR/utilization_synth.rpt"
set util [report_utilization -return_string]
foreach l [split $util "\n"] {
    if {[string match "*| Slice LUTs*" $l]} { puts "   $l" }
    if {[string match "*| Slice Registers*" $l]} { puts "   $l" }
    if {[string match "*| Block RAM Tile*" $l]} { puts "   $l" }
    if {[string match "*| RAMB18*" $l]} { puts "   $l" }
    if {[string match "*| DSPs*" $l]} { puts "   $l" }
}

puts "--- 4.5 时钟交互 / 跨时钟域 ---"
report_clock_interaction -file "$RPT_DIR/clock_interaction.rpt" -delay_type max
puts "  (已写入 clock_interaction.rpt)"


# ---------------------------------------------------------------------------
puts "===== S-05 汇总 ====="
puts "  综合状态 : $st"
puts "  WNS      : $wns ns"
puts "  WHS      : $whs ns"
puts "  报告目录 : $RPT_DIR"
puts "===== BUILD-SYNTH-OK ====="
exit
