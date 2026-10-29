# ============================================================================
#  S10 核查脚本：把已建好的 Block Design 完整"体检"一遍（只读，不修改、不保存）
#
#  用法： vivado -mode batch -nojournal -nolog -source ~/s10/verify_bd.tcl
#
#  输出分 V-01 .. V-10 十段，方便按段定位问题。
# ============================================================================

set HOME_DIR  $::env(HOME)
set PROJ_DIR  "$HOME_DIR/projects/vivado/vessel_sys"
set PROJ_NAME "vessel_sys"
set BD_NAME   "vessel_sys"


puts "===== V-01 打开工程 ====="
open_project "$PROJ_DIR/$PROJ_NAME.xpr"
puts "  器件 : [get_property part [current_project]]"
puts "  板卡 : [get_property board_part [current_project]]"


puts "===== V-02 打开 Block Design ====="
set bdf [get_files -quiet "$BD_NAME.bd"]
if {[llength $bdf] == 0} { error "V-02-FAIL: 工程里找不到 $BD_NAME.bd" }
open_bd_design [lindex $bdf 0]
puts "  BD 已打开"


puts "===== V-03 BD 内的单元清单 ====="
set all_cells [get_bd_cells]
puts "  单元总数: [llength $all_cells]"
foreach c $all_cells {
    set v "?"
    catch { set v [get_property VLNV $c] }
    puts "   [get_property NAME $c]   ($v)"
}


puts "===== V-04 对外引出的接口（板子上的物理接口）====="
set ports [get_bd_intf_ports]
puts "  接口总数: [llength $ports]"
foreach p $ports {
    set v "?"
    catch { set v [get_property VLNV $p] }
    puts "   [get_property NAME $p]   ($v)"
}


puts "===== V-05 接口之间的连线（这张图的接线表）====="
set inets [get_bd_intf_nets]
puts "  接口网络总数: [llength $inets]"
foreach net $inets {
    puts "  网络 [get_property NAME $net] :"
    foreach p [get_bd_intf_pins -quiet -of_objects $net] {
        puts "      - $p"
    }
}


puts "===== V-06 时钟 / 复位网络 ====="
foreach net [get_bd_nets] {
    set n [get_property NAME $net]
    if {[string match -nocase "*clk*" $n] || [string match -nocase "*rst*" $n] || [string match -nocase "*reset*" $n]} {
        puts "  网络 $n :"
        foreach p [get_bd_pins -quiet -of_objects $net] {
            puts "      - $p"
        }
    }
}

# ---- V-06b 复位辅助脚电平硬检查（2026-10-04 上板事故后新增）----
# aux_reset_in 默认低有效：接常 0 = 永久复位 = 上板首次总线访问整机挂死。
# 说明：只用「单元 -> 引脚 -> 网络」这个方向查询（Vivado 保证支持），
#       再比对网络名判断某根网络是不是由常量单元驱动。
#       ⚠ 不要用 get_property IP_NAME 判断 IP 类型（bd 单元上没有这个属性，
#          读出来是空 → 会误判 → 历史事故：核查脚本打出假的 "驱动=?"）。
#          正确做法是读 VLNV，如 xilinx.com:ip:xlconstant:1.1。
proc const_src_of {pin} {
    set net [get_bd_nets -quiet -of_objects $pin]
    if {[llength $net] == 0} { return "UNCONNECTED" }
    set netname [get_property -quiet NAME [lindex $net 0]]
    foreach c [get_bd_cells -quiet] {
        set vlnv ""
        catch { set vlnv [get_property -quiet VLNV $c] }
        if {![string match -nocase "*constant*" $vlnv]} { continue }
        foreach p [get_bd_pins -quiet -of_objects $c] {
            set n [get_bd_nets -quiet -of_objects $p]
            if {[llength $n] == 0} { continue }
            if {[get_property -quiet NAME [lindex $n 0]] eq $netname} {
                set cn "?"
                catch { set cn [get_property -quiet NAME $c] }
                set v "?"
                catch { set v [get_property -quiet CONFIG.CONST_VAL $c] }
                if {$v eq "" || $v eq "?"} { catch { set v [get_property -quiet CONST_VAL $c] } }
                return "$cn/$v"
            }
        }
    }
    return "?"
}

set p_aux [get_bd_pins -quiet proc_sys_reset_0/aux_reset_in]
if {[llength $p_aux]} {
    set pol "?"
    catch { set pol [get_property -quiet CONFIG.POLARITY $p_aux] }
    set src [const_src_of $p_aux]
    puts "  proc_sys_reset_0/aux_reset_in : POLARITY=$pol  驱动=$src"
    if {$pol eq "ACTIVE_LOW" && [string match "*const_zero_0/0" $src]} {
        error "V-06b-FAIL: aux_reset_in 低有效却接常 0 -> PL 永久复位，上板必挂！必须接常 1 或不接。"
    }
    if {$src eq "?"} {
        puts "  V-06b-WARN: 未能识别驱动源（既不是常量也不是空）-> 请人工确认上面那行「驱动=」的值"
    } else {
        puts "  V-06b-PASS: aux_reset_in 电平不会造成永久复位（驱动=$src）"
    }
} else {
    puts "  V-06b-PASS: aux_reset_in 不存在（新版本 IP），跳过"
}

set p_dbg [get_bd_pins -quiet proc_sys_reset_0/mb_debug_sys_rst]
if {[llength $p_dbg]} {
    set pol_d "?"
    catch { set pol_d [get_property -quiet CONFIG.POLARITY $p_dbg] }
    set src_d [const_src_of $p_dbg]
    puts "  proc_sys_reset_0/mb_debug_sys_rst : POLARITY=$pol_d  驱动=$src_d"
    if {$pol_d eq "ACTIVE_HIGH" && [string match "*const_one_0/1" $src_d]} {
        error "V-06b-FAIL: mb_debug_sys_rst 高有效却接常 1 -> 永久复位！必须接常 0。"
    }
    puts "  V-06b-PASS: mb_debug_sys_rst 电平不会造成永久复位（驱动=$src_d）"
}


puts "===== V-07 关键接口的位宽（核对 64bit 内存 / 8bit 流）====="
foreach ip [list processing_system7_0/M_AXI_GP0 processing_system7_0/S_AXI_HP0 \
                 ps7_0_axi_periph/S00_AXI ps7_0_axi_periph/M00_AXI ps7_0_axi_periph/M01_AXI \
                 axi_hp_ic/S00_AXI axi_hp_ic/S01_AXI axi_hp_ic/M00_AXI \
                 axi_dma_0/S_AXI_LITE axi_dma_0/M_AXI_MM2S axi_dma_0/M_AXI_S2MM \
                 axi_dma_0/M_AXIS_MM2S axi_dma_0/S_AXIS_S2MM \
                 vessel_top_0/src vessel_top_0/dst vessel_top_0/s_axi_control] {
    set p [get_bd_intf_pins -quiet $ip]
    if {[llength $p] == 0} { puts [format "  %-32s (不存在)" $ip] ; continue }
    set dw "?"
    set aw "?"
    catch { set dw [get_property CONFIG.DATA_WIDTH $p] }
    catch { set aw [get_property CONFIG.ADDR_WIDTH $p] }
    puts [format "  %-32s DATA_WIDTH=%s  ADDR_WIDTH=%s" $ip $dw $aw]
}


puts "===== V-08 DMA / 互连 / PS 的最终配置 ====="
foreach k {C_INCLUDE_SG C_SG_LENGTH_WIDTH C_M_AXI_MM2S_DATA_WIDTH C_M_AXIS_MM2S_TDATA_WIDTH \
           C_M_AXI_S2MM_DATA_WIDTH C_S_AXIS_S2MM_TDATA_WIDTH C_ADDR_WIDTH} {
    set v "?"
    catch { set v [get_property CONFIG.$k [get_bd_cells axi_dma_0]] }
    puts [format "  axi_dma_0.%-30s = %s" $k $v]
}

# ---- V-08b DMA 单次传输长度上限硬检查（2026-10-05 首次跑帧失败后新增）----
# AXI DMA 的 Width of Buffer Length Register 默认 14 位 -> 单次最多 2^14-1 = 16383 字节。
# 该参数在 simple（非 SG）模式下同样生效（AMD 官方答复）。
# 本项目一帧 = 605*700 = 423500 字节，14 位装不下，写入会被静默截断（423500 -> 13900），
# 导致 MM2S 提前发出 TLAST -> HLS IP 等不到完整帧而挂死 -> 总线读无响应 -> 整机冻死。
set FRAME_BYTES 423500
set lw "?"
catch { set lw [get_property CONFIG.C_SG_LENGTH_WIDTH [get_bd_cells axi_dma_0]] }
if {$lw eq "?" || $lw eq ""} {
    error "V-08b-FAIL: 读不到 C_SG_LENGTH_WIDTH，无法判定 DMA 单次传输上限。"
}
set lw_max [expr {int(2)**int($lw) - 1}]
puts [format "  一帧 %d 字节 ; DMA 长度寄存器 %s 位 -> 单次上限 %d 字节" $FRAME_BYTES $lw $lw_max]
if {$lw_max < $FRAME_BYTES} {
    error "V-08b-FAIL: DMA 单次最多 $lw_max 字节 < 一帧 $FRAME_BYTES 字节，传输会被截断！请把 c_sg_length_width 提到 19 位以上（建议 26）。"
}
puts "  V-08b-PASS: DMA 单次传输上限足以覆盖一整帧"
foreach c {ps7_0_axi_periph axi_hp_ic} {
    foreach k {NUM_SI NUM_MI} {
        set v "?"
        catch { set v [get_property CONFIG.$k [get_bd_cells $c]] }
        puts [format "  %-18s.%-10s = %s" $c $k $v]
    }
}
foreach k {PCW_FPGA0_PERIPHERAL_FREQMHZ PCW_S_AXI_HP0_FREQMHZ PCW_M_AXI_GP0_FREQMHZ \
           PCW_USE_S_AXI_HP0 PCW_S_AXI_HP0_DATA_WIDTH PCW_USE_M_AXI_GP0} {
    set v "?"
    catch { set v [get_property CONFIG.$k [get_bd_cells processing_system7_0]] }
    puts [format "  processing_system7_0.%-30s = %s" $k $v]
}


puts "===== V-09 未连接的引脚（只列独立信号脚，接口成员脚不算）====="
puts "  说明：接口已连通的端口，其成员信号脚在 pin 层面查不到网，属正常，故排除。"
foreach c {vessel_top_0 axi_dma_0} {
    set cell [get_bd_cells $c]
    set intf_names {}
    foreach ip [get_bd_intf_pins -quiet -of_objects $cell] {
        catch { lappend intf_names [get_property NAME $ip] }
    }
    set n_bad 0
    foreach p [get_bd_pins -quiet -of_objects $cell] {
        set pn [get_property NAME $p]
        set member 0
        foreach in $intf_names {
            if {[string match -nocase "${in}_*" $pn]} { set member 1 ; break }
        }
        if {$member} { continue }
        if {[llength [get_bd_nets -quiet -of_objects $p]] == 0} {
            set d "?"
            catch { set d [get_property DIR $p] }
            puts "  未连接($d): $p"
            incr n_bad
        }
    }
    if {$n_bad == 0} { puts "  $c : 独立信号脚全部已连接" }
}


puts "===== V-10 地址映射 ====="
set n_seg 0
foreach s [get_bd_addr_segs] {
    set off "?"
    set rng "?"
    catch { set off [get_property -quiet offset $s] }
    catch { set rng [get_property -quiet range $s] }
    if {$off eq ""} { set off "(未分配)" }
    if {$rng eq ""} { set rng "(无)" }
    puts [format "  %-72s offset=%-12s size=%s" $s $off $rng]
    incr n_seg
}
puts "  地址段总数: $n_seg"


puts "===== V-11 AXI4-Stream 数据位宽（两端必须一致）====="
foreach ip {axi_dma_0/M_AXIS_MM2S vessel_top_0/src vessel_top_0/dst axi_dma_0/S_AXIS_S2MM} {
    set p [get_bd_intf_pins -quiet $ip]
    if {[llength $p] == 0} { puts "  $ip  (不存在)" ; continue }
    set nb "?"
    foreach prop {CONFIG.TDATA_NUM_BYTES CONFIG.TDATA_WIDTH} {
        set v "?"
        catch { set v [get_property $prop $p] }
        if {$v ne "" && $v ne "?"} { set nb $v ; break }
    }
    set tk "?"
    catch { set tk [get_property CONFIG.HAS_TKEEP $p] }
    puts [format "  %-28s TDATA_NUM_BYTES=%s  HAS_TKEEP=%s" $ip $nb $tk]
}


puts "===== V-12 时钟域检查（PS-PL 接口频率）====="
foreach k {PCW_FPGA0_PERIPHERAL_FREQMHZ PCW_M_AXI_GP0_FREQMHZ PCW_S_AXI_HP0_FREQMHZ PCW_USE_FABRIC_INTERRUPT} {
    set v "?"
    catch { set v [get_property CONFIG.$k [get_bd_cells processing_system7_0]] }
    puts [format "  processing_system7_0.%-30s = %s" $k $v]
}


puts "===== V-99 核查结束 ====="
exit
