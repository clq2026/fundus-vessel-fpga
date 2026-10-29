@echo off
REM ============================================================
REM  本地 C++ 仿真（用普通 g++，无需 Vitis HLS）
REM  在本文件所在目录 vessel_prj 下双击或命令行运行
REM ============================================================
setlocal
set "GPP=g++"
set "TV_DIR=..\test_vectors"

where "%GPP%" >nul 2>nul
if errorlevel 1 (
  echo [错误] 没在 PATH 里找到 g++。
  echo        请先安装 MinGW-w64 / TDM-GCC，或把本脚本里的 GPP 改成你的 g++ 完整路径。
  pause
  exit /b 1
)

echo [1/2] 编译...
"%GPP%" -O2 -std=c++14 -I native_sim\stubs -I . tb_vessel.cpp vessel_top.cpp -o native_sim\vessel_sim.exe
if errorlevel 1 ( echo [编译失败] & pause & exit /b 1 )

echo [2/2] 运行 C++ 仿真（逐像素对比 Python 参考）...
native_sim\vessel_sim.exe
pause
