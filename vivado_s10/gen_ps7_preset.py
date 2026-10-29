# -*- coding: utf-8 -*-
"""
把 PYNQ-Z2 板卡文件里的 ps7_preset（771 个 processing_system7 参数）
转换成 Vivado Tcl 的 set_property 配置代码。

用法：
    python3 gen_ps7_preset.py <输入 preset.xml> <输出 .tcl>
不传参数时，默认在当前目录找 preset.xml、输出 ps7_preset_params.tcl。
"""
import os
import sys
import xml.etree.ElementTree as ET

SRC = sys.argv[1] if len(sys.argv) > 1 else "preset.xml"
DST = sys.argv[2] if len(sys.argv) > 2 else "ps7_preset_params.tcl"


def main():
    tree = ET.parse(SRC)
    root = tree.getroot()

    target = None
    for preset in root.findall("ip_preset"):
        if preset.get("preset_proc_name") != "ps7_preset":
            continue
        for ip in preset.findall("ip"):
            if ip.get("name") == "processing_system7":
                target = ip
                break
    if target is None:
        raise SystemExit("ERROR: 没找到 ps7_preset / processing_system7 块")

    params = []
    for up in target.findall("user_parameters/user_parameter"):
        name = (up.get("name") or "").strip()
        value = up.get("value")
        value = "" if value is None else value.strip()
        if not name:
            continue
        params.append((name, value))

    # ---- 安全检查：Tcl 花括号包裹时，值里不能有未配对的 { } 或反斜杠 ----
    problems = []
    for name, value in params:
        for field in (name, value):
            if "{" in field or "}" in field or "\\" in field or "\n" in field:
                problems.append((name, value))
                break
    if problems:
        print("!! 发现无法直接用花括号包裹的参数（需转义）：")
        for p in problems[:20]:
            print("   ", p)
    else:
        print("安全检查通过：所有参数都能安全地用 Tcl 花括号包裹")

    # ---- 写 Tcl ----
    lines = []
    lines.append("# ============================================================================")
    lines.append("#  自动生成：PYNQ-Z2 板卡的 processing_system7 预设参数（来自官方 preset.xml）")
    lines.append("#  来源: pynq-z2/A.0/preset.xml  ->  ip_preset@ps7_preset  ->  xilinx.com:ip:processing_system7")
    lines.append("#  参数个数: %d" % len(params))
    lines.append("#  用法：此文件由 build_bd.tcl 用 source 载入，不要单独运行")
    lines.append("# ============================================================================")
    lines.append("")
    lines.append("set ps7_cfg [list]")
    for name, value in params:
        lines.append("lappend ps7_cfg {%s} {%s}" % (name, value))
    lines.append("")
    lines.append('puts "PS7 预设参数已装载: [expr {[llength $ps7_cfg]/2}] 条"')
    lines.append("")

    with open(DST, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))

    print("已写出:", DST)
    print("参数量:", len(params), " 文件大小:", os.path.getsize(DST), "字节")

    # ---- 打印关键参数，便于人工核对 ----
    d = dict(params)
    keys = [
        "CONFIG.PCW_UIPARAM_DDR_PARTNO",
        "CONFIG.PCW_UIPARAM_DDR_BUS_WIDTH",
        "CONFIG.PCW_UIPARAM_DDR_FREQ_MHZ",
        "CONFIG.PCW_UIPARAM_DDR_ENABLE",
        "CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ",
        "CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ",
        "CONFIG.PCW_USE_M_AXI_GP0",
        "CONFIG.PCW_USE_S_AXI_HP0",
        "CONFIG.PCW_S_AXI_HP0_DATA_WIDTH",
        "CONFIG.PCW_EN_CLK0_PORT",
        "CONFIG.PCW_UART1_PERIPHERAL_ENABLE",
        "CONFIG.PCW_SD0_PERIPHERAL_ENABLE",
        "CONFIG.PCW_ENET0_PERIPHERAL_ENABLE",
        "CONFIG.PCW_USB0_PERIPHERAL_ENABLE",
        "CONFIG.PCW_USE_FABRIC_INTERRUPT",
    ]
    print("\n===== 关键参数核对 =====")
    for k in keys:
        print("%-45s = %s" % (k, d.get(k, "(预设中未出现)")))

    print("\n===== 参数名前缀分布 =====")
    from collections import Counter
    c = Counter(n.split("_")[1] for n, _ in params)
    for k, v in c.most_common(15):
        print("  %-16s %d" % (k, v))


if __name__ == "__main__":
    main()
