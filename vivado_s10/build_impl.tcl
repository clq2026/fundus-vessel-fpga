# =============================================================================
#  S10 阶段三：实现（impl_1：opt / place / route）+ 生成 bitstream + 只读报告
#
#  用法（在 Linux 终端）：
#     vivado -mode batch -nojournal -nolog -source ~/s10/build_impl.tcl
#
#  本脚本做【实现 + 出 bitstream】，不做板上验证。
#  重点确认：
#     1) impl 有没有报错、DRC 有没有 Critical 违规；
#     2) 布局布线后的真实 WNS（这才是最终时序，综合值只是估算）；
#     3) post-route 资源占用；
#     4) 有没有生成 vessel_sys_wrapper.bit
#
#  报告文件写到 ~/s10/report/
#  预计 20-40 分钟，期间不要关终端、不要关虚拟机、不要断电。
# =============================================================================

set HOME_DIR  $::env(HOME)
set PROJ_DIR  "$HOME_DIR/projects/vivado/vessel_sys"
set PROJ_NAME "vessel_sys"
set RPT_DIR   "$HOME_DIR/s10/report"

file mkdir $RPT_DIR


# ---------------------------------------------------------------------------
puts "===== I-01 打开工程 ====="
open_project "$PROJ_DIR/$PROJ_NAME.xpr"
puts "  器件 : [get_property part [current_project]]"
puts "  板卡 : [get_property board_part [current_project]]"


# ---------------------------------------------------------------------------
puts "===== I-02 确认综合已完成 ====="
set sst "?"
catch { set sst [get_property STATUS [get_runs synth_1]] }
puts "  synth_1 状态 : $sst"
if {![string match "*Complete*" $sst]} { error "I-02-FAIL: synth_1 未完成，不能进入实现" }


# ---------------------------------------------------------------------------
puts "===== I-03 启动实现到 write_bitstream（不要关终端）====="
reset_run impl_1
if {[catch { launch_runs impl_1 -to_step write_bitstream -jobs 4 } err]} {
    puts "!! launch_runs impl_1: $err"
}
wait_on_run impl_1
set ist "?"
set ipr "?"
catch { set ist [get_property STATUS [get_runs impl_1]] }
catch { set ipr [get_property PROGRESS [get_runs impl_1]] }
puts "  impl_1 状态 : $ist"
puts "  impl_1 进度 : $ipr"

if {![string match "*Complete*" $ist]} {
    puts "!! 实现未正常完成，状态=$ist —— 不再继续"
    exit 1
}


# ---------------------------------------------------------------------------
puts "===== I-04 打开实现结果（post-route）====="
if {[catch { open_run impl_1 } err]} {
    puts "!! open_run impl_1 失败: $err"
    exit 1
}
puts "  已打开 impl_1"


# ---------------------------------------------------------------------------
puts "===== I-05 时序（布局布线后的最终值）====="
report_timing_summary -file "$RPT_DIR/timing_impl.rpt" -delay_type max

set wp [get_timing_paths -delay_type max -max_paths 1 -quiet]
set wns "?"
set wns_src "?"
set wns_dst "?"
if {[llength $wp] > 0} {
    set p0 [lindex $wp 0]
    catch { set wns     [get_property SLACK $p0] }
    catch { set wns_src [get_property STARTPOINT_PIN $p0] }
    catch { set wns_dst [get_property ENDPOINT_PIN $p0] }
}
set wph [get_timing_paths -delay_type min -max_paths 1 -quiet]
set whs "?"
if {[llength $wph] > 0} { catch { set whs [get_property SLACK [lindex $wph 0]] } }

puts "  建立(setup) WNS = $wns ns"
puts "     起点: $wns_src"
puts "     终点: $wns_dst"
puts "  保持(hold)  WHS = $whs ns"

set tsr [report_timing_summary -return_string]
if {[string match "*All user specified timing constraints are met*" $tsr]} {
    puts "  >>> 时序约束: 全部满足 (Timing MET)"
} else {
    puts "  !! 时序约束: 未全部满足 —— 请看 timing_impl.rpt，必要时降频到 90MHz"
}


# ---------------------------------------------------------------------------
puts "===== I-06 资源占用（post-route）====="
report_utilization -file "$RPT_DIR/utilization_impl.rpt"
set util [report_utilization -return_string]
foreach l [split $util "\n"] {
    if {[string match "*| Slice LUTs*" $l]}      { puts "   $l" }
    if {[string match "*| Slice Registers*" $l]} { puts "   $l" }
    if {[string match "*| Block RAM Tile*" $l]}  { puts "   $l" }
    if {[string match "*| DSPs*" $l]}            { puts "   $l" }
}


# ---------------------------------------------------------------------------
puts "===== I-07 DRC 检查 ====="
report_drc -file "$RPT_DIR/drc_impl.rpt"
array set SEV {Critical 0 Warning 0 Advisory 0 Info 0}
foreach v [get_drc_violations -quiet] {
    catch {
        set s [get_property SEVERITY $v]
        if {[info exists SEV($s)]} { incr SEV($s) }
    }
}
puts "  DRC 违规: Critical=$SEV(Critical)  Warning=$SEV(Warning)  Advisory=$SEV(Advisory)"
if {$SEV(Critical) > 0} {
    puts "  !! 存在 Critical 违规 —— 若 bitstream 未生成，多半是它导致，请看 drc_impl.rpt"
}


# ---------------------------------------------------------------------------
puts "===== I-08 定位 bitstream ====="
set bits [glob -nocomplain "$PROJ_DIR/$PROJ_NAME.runs/impl_1/*.bit"]
if {[llength $bits] == 0} {
    puts "  !! 没找到 .bit 文件 —— bitstream 可能没生成成功"
} else {
    foreach b $bits {
        puts "  bitstream : $b"
        catch { puts "     大小 : [file size $b] 字节" }
    }
}


# ---------------------------------------------------------------------------
puts "===== I-09 汇总 ====="
puts "  impl 状态 : $ist"
puts "  WNS       : $wns ns"
puts "  WHS       : $whs ns"
puts "  DRC       : Critical=$SEV(Critical) Warning=$SEV(Warning)"
puts "  报告目录  : $RPT_DIR"
puts "===== BUILD-IMPL-OK ====="
exit
