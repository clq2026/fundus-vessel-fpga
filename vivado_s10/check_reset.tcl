# ============================================================================
#  复位连线取证脚本（只打印原始事实，不做任何判断，只读不改）
#
#  用法： vivado -mode batch -nojournal -nolog -source ~/s10/check_reset.tcl
#
#  目的：打印 proc_sys_reset_0 各复位脚挂在哪根网络、以及两个 xlconstant
#        的常量值分别流向哪里 —— 用来确认 aux_reset_in 到底接的是 0 还是 1。
#  说明：全部查询都用「从单元查引脚」这个方向（Vivado 保证支持），
#        避免使用 get_bd_cells -of_objects <引脚> 这类不稳的写法。
# ============================================================================

set HOME_DIR  $::env(HOME)
set PROJ_DIR  "$HOME_DIR/projects/vivado/vessel_sys"
set PROJ_NAME "vessel_sys"
set BD_NAME   "vessel_sys"

puts "===== R-00 打开工程与 BD ====="
open_project "$PROJ_DIR/$PROJ_NAME.xpr"
set bdf [get_files -quiet "$BD_NAME.bd"]
if {[llength $bdf] == 0} { error "R-00-FAIL: 找不到 $BD_NAME.bd" }
open_bd_design [lindex $bdf 0]
puts "  BD 已打开"


puts "===== R-01 所有 xlconstant 常量单元的取值 ====="
set const_cells {}
foreach c [get_bd_cells -quiet] {
    set ipn "?"
    catch { set ipn [get_property -quiet VLNV $c] }
    if {[string match -nocase "*constant*" $ipn]} {
        set cn [get_property NAME $c]
        set v "?"
        catch { set v [get_property -quiet CONFIG.CONST_VAL $c] }
        if {$v eq "" || $v eq "?"} { catch { set v [get_property -quiet CONST_VAL $c] } }
        puts "  常量单元 $cn : IP=$ipn  CONST_VAL=$v"
        lappend const_cells $c
    }
}
if {[llength $const_cells] == 0} { puts "  (没找到任何常量单元)" }


puts "===== R-02 复位模块 proc_sys_reset_0 每个脚 -> 网络 ====="
set rst_cell [get_bd_cells -quiet proc_sys_reset_0]
if {[llength $rst_cell] == 0} {
    puts "  (找不到 proc_sys_reset_0)"
} else {
    foreach p [get_bd_pins -quiet -of_objects $rst_cell] {
        set pn [get_property -quiet NAME $p]
        set dir [get_property -quiet DIR $p]
        set net [get_bd_nets -quiet -of_objects $p]
        set netname "(未连接)"
        if {[llength $net]} { set netname [get_property NAME [lindex $net 0]] }
        set pol "?"
        catch { set pol [get_property -quiet CONFIG.POLARITY $p] }
        puts [format "  脚 %-20s DIR=%s POLARITY=%-12s 网络=%s" $pn $dir $pol $netname]
    }
}


puts "===== R-03 每个常量单元的 dout 流向哪些脚 ====="
foreach c $const_cells {
    set cn [get_property NAME $c]
    set v "?"
    catch { set v [get_property -quiet CONFIG.CONST_VAL $c] }
    foreach p [get_bd_pins -quiet -of_objects $c] {
        set pn [get_property -quiet NAME $p]
        set net [get_bd_nets -quiet -of_objects $p]
        if {[llength $net] == 0} {
            puts "  $cn (值=$v) 脚 $pn -> (未连接任何网络)"
            continue
        }
        set dst {}
        foreach dp [get_bd_pins -quiet -of_objects [lindex $net 0]] {
            lappend dst "$dp"
        }
        puts "  $cn (值=$v) 脚 $pn -> 网络 [get_property NAME [lindex $net 0]]"
        puts "      该网络上的全部引脚: $dst"
    }
}


puts "===== R-99 取证结束（以上为原始事实，未做判断）====="
exit
