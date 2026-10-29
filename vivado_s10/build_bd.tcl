# ============================================================================
#  S10 第一步：建立 Vivado 工程 + Block Design
#    Zynq PS(PYNQ-Z2 板卡预设) + AXI DMA(64bit 内存 / 8bit 流) + 血管 IP
#
#  用法（在 Linux 虚拟机终端里）：
#     vivado -mode batch -nojournal -nolog -source ~/s10/build_bd.tcl
#
#  依赖：同目录下的 ps7_preset_params.tcl（由板卡 preset.xml 自动生成）
#  本脚本可重复运行：每次都会删掉旧工程重建，约 1 分钟。
# ============================================================================

set HOME_DIR  $::env(HOME)
set PROJ_DIR  "$HOME_DIR/projects/vivado/vessel_sys"
set PROJ_NAME "vessel_sys"
set BD_NAME   "vessel_sys"
set IP_REPO   "$HOME_DIR/projects/hls/vessel_prj/vessel_prj/solution1/impl/ip"
set BOARD     "tul.com.tw:pynq-z2:part0:1.0"
set PART      "xc7z020clg400-1"
set PRESET_TCL "$HOME_DIR/s10/ps7_preset_params.tcl"


# ---------------------------------------------------------------------------
#  工具过程
# ---------------------------------------------------------------------------

# 把某个 cell 上所有"输入方向"的时钟/复位脚一次性接好
proc hook_clk_rst {cells clk rst} {
    foreach c $cells {
        set cell [get_bd_cells -quiet $c]
        if {[llength $cell] == 0} { continue }
        foreach p [get_bd_pins -quiet -of_objects $cell] {
            if {[get_property -quiet DIR $p] ne "I"} { continue }
            set n [get_property NAME $p]
            if {[string match -nocase "*aclk*" $n]}   { connect_bd_net $clk $p }
            # 注意：AXI DMA 的复位脚叫 axi_resetn（名字里没有 "aresetn"），
            # 所以这里必须用 *resetn*，否则 DMA 的复位脚会被漏掉、
            # 上板后 DMA 永远停在复位状态（症状：寄存器读得到但传输不启动）。
            if {[string match -nocase "*resetn*" $n]} { connect_bd_net $rst $p }
        }
    }
}

# AXI4-Stream 逐信号连接（接口整体连接失败时的备选方案）
proc stream_connect {m s} {
    foreach sig {TDATA TVALID TREADY TLAST TKEEP TSTRB TUSER} {
        set a [get_bd_pins -quiet $m/$sig]
        set b [get_bd_pins -quiet $s/$sig]
        if {[llength $a] == 0 && [llength $b] == 0} { continue }
        if {[llength $a] == 0} { puts "      (跳过 $sig：主端没有此信号)"; continue }
        if {[llength $b] == 0} { puts "      (跳过 $sig：从端没有此信号)"; continue }
        if {[catch { connect_bd_net $a $b } e]} {
            if {[catch { connect_bd_net $b $a } e2]} {
                puts "      连接 $sig 失败: $e2"
            } else {
                puts "      连接 $sig（反向）"
            }
        } else {
            puts "      连接 $sig"
        }
    }
}


# ---------------------------------------------------------------------------
puts "===== MARK-01 建立工程 ====="
if {[file exists $PROJ_DIR]} {
    puts "删除旧工程目录: $PROJ_DIR"
    file delete -force $PROJ_DIR
}
file mkdir $PROJ_DIR
create_project $PROJ_NAME $PROJ_DIR -part $PART -force
set_property board_part $BOARD [current_project]
puts "工程目录 : [get_property DIRECTORY [current_project]]"
puts "器件     : [get_property part [current_project]]"
puts "板卡     : [get_property board_part [current_project]]"


# ---------------------------------------------------------------------------
puts "===== MARK-02 导入血管 IP ====="
set_property ip_repo_paths $IP_REPO [current_project]
if {[catch { update_ip_catalog -rebuild } err]} { puts "!! update_ip_catalog: $err" }

set VLNV_LIST [get_ipdefs -all *vessel_top*]
puts "匹配到的 IP 定义: [llength $VLNV_LIST] 条"
foreach v $VLNV_LIST { puts "   -> $v" }
if {[llength $VLNV_LIST] == 0} { error "MARK-02-FAIL: 在 $IP_REPO 没找到 vessel_top" }
set VLNV ""
foreach v $VLNV_LIST { if {[string match "*:hls:*" $v]} { set VLNV $v ; break } }
if {$VLNV eq ""} { set VLNV [lindex $VLNV_LIST 0] }
puts "采用 VLNV: $VLNV"


# ---------------------------------------------------------------------------
puts "===== MARK-03 建立 Block Design + 配置 Zynq PS ====="
create_bd_design $BD_NAME

set PS [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 processing_system7_0]
puts "PS cell 已建立"

puts "--- 3.1 尝试板卡自动化（Run Block Automation）---"
set auto_ok 0
if {[catch {
    apply_bd_automation -rule xilinx.com:bd_rule:processing_system7 \
        -config {make_external "FIXED_IO, DDR" apply_board_preset "1"} $PS
} err]} {
    puts "!! 板卡自动化未成功（不要紧，下一步手工套用官方参数）:"
    puts "   $err"
} else {
    set auto_ok 1
    puts "板卡自动化执行成功"
}

puts "--- 3.2 套用官方 preset.xml 提取出来的 PS 参数 ---"
if {[file exists $PRESET_TCL]} {
    source $PRESET_TCL

    # 过滤掉【只读】的 AXI 端口频率参数：
    #   PCW_M_AXI_*_FREQMHZ / PCW_S_AXI_*_FREQMHZ（共 9 条）
    # 它们在 Vivado 2022.2 里由 IP 自动传播推导，手工写会被拒并刷 9 条
    # BD 41-737 CRITICAL WARNING；而且板卡预设给的是占位值 10，反而会挡住自动推导。
    set kept {}
    set dropped 0
    foreach {k v} $ps7_cfg {
        if {[string match "*PCW_M_AXI_*_FREQMHZ" $k] || [string match "*PCW_S_AXI_*_FREQMHZ" $k]} {
            incr dropped
            continue
        }
        lappend kept $k $v
    }
    set ps7_cfg $kept
    puts "   已剔除只读的 AXI 端口频率参数 $dropped 条（交给 Vivado 自动推导）"

    if {[catch { set_property -dict $ps7_cfg $PS } err]} {
        puts "!! 一次性套用失败，改为逐条套用（会慢一些）"
        set bad 0
        foreach {k v} $ps7_cfg {
            if {[catch { set_property $k $v $PS } e]} { incr bad }
        }
        puts "   逐条套用完成，被拒绝的参数: $bad 条"
    } else {
        puts "   已一次性套用 [expr {[llength $ps7_cfg]/2}] 条 PS 参数"
    }
} else {
    puts "!! 找不到 $PRESET_TCL —— PS 将只有自动化配置"
}

puts "--- 3.3 覆盖我们设计需要的选项 ---"
set ps_over [list \
    CONFIG.PCW_USE_M_AXI_GP0 {1} \
    CONFIG.PCW_USE_S_AXI_HP0 {1} \
    CONFIG.PCW_S_AXI_HP0_DATA_WIDTH {64} \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100.000000} ]
if {[catch { set_property -dict $ps_over $PS } err]} { puts "!! PS 覆盖失败: $err" }

# 关于 AXI 端口的"工作频率"参数（PCW_M_AXI_GP0_FREQMHZ / PCW_S_AXI_HP0_FREQMHZ）：
#   这两个在 Vivado 2022.2 里是【只读】参数，由 IP 自动传播推导，手工写会被拒绝。
#   真正的时钟频率由 MARK-07 里 FCLK_CLK0（100MHz）的连线决定。
#   这里只把当前值打出来备查；等读 Timing Summary 的时钟表时再复核一次。
foreach k {PCW_S_AXI_HP0_FREQMHZ PCW_M_AXI_GP0_FREQMHZ} {
    set v "?"
    catch { set v [get_property CONFIG.$k $PS] }
    puts [format "   %-28s = %s   (只读，由 Vivado 传播)" $k $v]
}

puts "--- 3.4 PS 关键配置核对 ---"
foreach k {PCW_UIPARAM_DDR_PARTNO PCW_UIPARAM_DDR_BUS_WIDTH PCW_UIPARAM_DDR_FREQ_MHZ \
           PCW_FPGA0_PERIPHERAL_FREQMHZ PCW_USE_M_AXI_GP0 PCW_USE_S_AXI_HP0 \
           PCW_S_AXI_HP0_DATA_WIDTH PCW_S_AXI_HP0_FREQMHZ PCW_M_AXI_GP0_FREQMHZ \
           PCW_SD0_PERIPHERAL_ENABLE PCW_ENET0_PERIPHERAL_ENABLE} {
    puts [format "   %-36s = %s" $k [get_property CONFIG.$k $PS]]
}

puts "--- 3.5 确保 DDR / FIXED_IO 已引出为外部接口 ---"
foreach itf {DDR FIXED_IO} {
    set p [get_bd_intf_pins -quiet processing_system7_0/$itf]
    if {[llength $p] == 0} { puts "   ($itf 不存在)"; continue }
    if {[llength [get_bd_intf_nets -quiet -of_objects $p]] == 0} {
        if {[catch { make_bd_intf_pins_external $p } e]} {
            puts "   引出 $itf 失败: $e"
        } else {
            puts "   已把 $itf 引出为外部接口"
        }
    } else {
        puts "   $itf 已是外部接口"
    }
}


# ---------------------------------------------------------------------------
puts "===== MARK-04 加入 AXI DMA ====="
set DMA [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 axi_dma_0]
set dma_cfg [list \
    CONFIG.c_include_sg {0} \
    CONFIG.c_sg_length_width {26} \
    CONFIG.c_sg_include_stscntrl_strm {0} \
    CONFIG.c_include_mm2s {1} \
    CONFIG.c_include_s2mm {1} \
    CONFIG.c_m_axi_mm2s_data_width {64} \
    CONFIG.c_m_axis_mm2s_tdata_width {8} \
    CONFIG.c_m_axi_s2mm_data_width {64} \
    CONFIG.c_s_axis_s2mm_tdata_width {8} \
    CONFIG.c_addr_width {32} \
    CONFIG.c_mm2s_burst_size {256} \
    CONFIG.c_s2mm_burst_size {256} ]
if {[catch { set_property -dict $dma_cfg $DMA } err]} {
    puts "!! DMA 参数设置失败: $err"
} else {
    puts "DMA 配置成功"
}
foreach k {c_include_sg c_sg_length_width c_m_axi_mm2s_data_width c_m_axis_mm2s_tdata_width \
           c_m_axi_s2mm_data_width c_s_axis_s2mm_tdata_width c_addr_width} {
    puts [format "   %-34s = %s" $k [get_property CONFIG.$k $DMA]]
}


# ---------------------------------------------------------------------------
puts "===== MARK-05 加入血管 IP ====="
set VT [create_bd_cell -type ip -vlnv $VLNV vessel_top_0]
puts "  接口(AXI 接口脚):"
foreach i [get_bd_intf_pins -of_objects $VT] { puts "     $i" }
puts "  引脚(非 s_axi_control 的信号脚):"
set nsaxi 0
foreach p [get_bd_pins -of_objects $VT] {
    set n [get_property NAME $p]
    if {[string match "s_axi_control*" $n]} { incr nsaxi ; continue }
    puts "     $n"
}
puts "  (另有 s_axi_control_* 信号脚 $nsaxi 条，已省略)"


# ---------------------------------------------------------------------------
puts "===== MARK-06 互连与复位 ====="
set IC_CTRL [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 ps7_0_axi_periph]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {2}] $IC_CTRL
puts "控制通路互连: 1 主 -> 2 从"

set IC_HP [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 axi_hp_ic]
set_property -dict [list CONFIG.NUM_SI {2} CONFIG.NUM_MI {1}] $IC_HP
puts "HP 数据通路互连: 2 主 -> 1 从"

set RST [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 proc_sys_reset_0]
puts "复位模块已建立"

# 复位模块的三个"辅助输入"必须显式接上，否则复位可能永远不放行：
#   aux_reset_in   -> 常 1（本 IP 默认低有效 C_AUX_RESET_HIGH=0，接 0 会永久锁死复位！）
#   mb_debug_sys_rst -> 常 0（高有效，0 = 不触发）
#   dcm_locked     -> 常 1（时钟已稳定，允许放行）
# ⚠ 2026-10-04 上板排障实锤：aux_reset_in 曾误接常 0 → PL 全部 AXI 从设备永久复位
#   → 首次总线读挂死整机。官方 base.hwh 里 ACTIVE_LOW 的 aux_reset_in 一律不接或接无效电平。
set C0 [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 const_zero_0]
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {0}] $C0
set C1 [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 const_one_0]
set_property -dict [list CONFIG.CONST_WIDTH {1} CONFIG.CONST_VAL {1}] $C1
set p [get_bd_pins -quiet proc_sys_reset_0/aux_reset_in]
if {[llength $p]} { connect_bd_net [get_bd_pins const_one_0/dout] $p ; puts "  常1 -> proc_sys_reset.aux_reset_in (低有效=不触发)" }
set p [get_bd_pins -quiet proc_sys_reset_0/mb_debug_sys_rst]
if {[llength $p]} { connect_bd_net [get_bd_pins const_zero_0/dout] $p ; puts "  常0 -> proc_sys_reset.mb_debug_sys_rst (高有效=不触发)" }
set p [get_bd_pins -quiet proc_sys_reset_0/dcm_locked]
if {[llength $p]} { connect_bd_net [get_bd_pins const_one_0/dout] $p ; puts "  常1 -> proc_sys_reset.dcm_locked" }


# ---------------------------------------------------------------------------
puts "===== MARK-07 时钟与复位连线 ====="
set CLK [get_bd_pins processing_system7_0/FCLK_CLK0]
set ARN [get_bd_pins proc_sys_reset_0/peripheral_aresetn]

connect_bd_net [get_bd_pins processing_system7_0/FCLK_RESET0_N] [get_bd_pins proc_sys_reset_0/ext_reset_in]
puts "  FCLK_RESET0_N -> proc_sys_reset.ext_reset_in"

connect_bd_net $CLK [get_bd_pins proc_sys_reset_0/slowest_sync_clk]
puts "  FCLK_CLK0 -> proc_sys_reset.slowest_sync_clk"

hook_clk_rst {processing_system7_0 axi_dma_0 ps7_0_axi_periph axi_hp_ic} $CLK $ARN
puts "  已批量接好 PS / DMA / 两个互连的时钟与复位"

connect_bd_net $CLK [get_bd_pins vessel_top_0/ap_clk]
connect_bd_net $ARN [get_bd_pins vessel_top_0/ap_rst_n]
puts "  FCLK_CLK0 -> vessel_top.ap_clk ；peripheral_aresetn -> vessel_top.ap_rst_n"


# ---------------------------------------------------------------------------
puts "===== MARK-08 AXI 接口连线 ====="
set axi_pairs [list \
    [list processing_system7_0/M_AXI_GP0  ps7_0_axi_periph/S00_AXI] \
    [list ps7_0_axi_periph/M00_AXI       axi_dma_0/S_AXI_LITE] \
    [list ps7_0_axi_periph/M01_AXI       vessel_top_0/s_axi_control] \
    [list axi_dma_0/M_AXI_MM2S           axi_hp_ic/S00_AXI] \
    [list axi_dma_0/M_AXI_S2MM           axi_hp_ic/S01_AXI] \
    [list axi_hp_ic/M00_AXI              processing_system7_0/S_AXI_HP0] ]
foreach pr $axi_pairs {
    lassign $pr a b
    if {[catch { connect_bd_intf_net [get_bd_intf_pins $a] [get_bd_intf_pins $b] } e]} {
        puts "!! 连接 $a -> $b 失败: $e"
    } else {
        puts "  OK  $a  ->  $b"
    }
}


# ---------------------------------------------------------------------------
puts "===== MARK-09 AXI4-Stream 连线 ====="
set spairs [list \
    [list axi_dma_0/M_AXIS_MM2S vessel_top_0/src] \
    [list vessel_top_0/dst     axi_dma_0/S_AXIS_S2MM] ]
foreach pr $spairs {
    lassign $pr m s
    if {[catch { connect_bd_intf_net [get_bd_intf_pins $m] [get_bd_intf_pins $s] } e]} {
        puts "  接口整体连接失败（$m -> $s），改用逐信号连接："
        puts "   原因: $e"
        stream_connect $m $s
    } else {
        puts "  OK  $m  ->  $s"
    }
}


# ---------------------------------------------------------------------------
puts "===== MARK-10 分配地址 ====="
if {[catch { assign_bd_address -quiet } err]} { puts "!! assign_bd_address: $err" }
puts "  地址映射："
foreach s [get_bd_addr_segs] {
    set off [get_property -quiet offset $s]
    set rng [get_property -quiet range  $s]
    if {$off eq ""} { set off "(未分配)" }
    puts [format "    %-72s offset=%-12s size=%s" $s $off $rng]
}


# ---------------------------------------------------------------------------
puts "===== MARK-11 校验 Block Design ====="
if {[catch { validate_bd_design } err]} {
    puts "!! validate_bd_design 抛出异常: $err"
} else {
    puts "  validate_bd_design 执行完毕（上面若有 CRITICAL WARNING 需留意）"
}

save_bd_design
puts "  BD 已保存"


# ---------------------------------------------------------------------------
puts "===== MARK-12 汇总 ====="
puts "  工程文件 : $PROJ_DIR/$PROJ_NAME.xpr"
puts "  BD 文件  : $PROJ_DIR/$PROJ_NAME.srcs/sources_1/bd/$BD_NAME/$BD_NAME.bd"
puts "  BD 内单元:"
foreach c [get_bd_cells] { puts "     $c" }
puts "===== 全部完成 ====="

exit
