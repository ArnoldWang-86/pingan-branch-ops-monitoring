# 一键复现（PowerShell 版，等价于 run_all.bat）
#
# 本文件保存为「UTF-8 with BOM」。
# 原因：Windows PowerShell 5.1 对无 BOM 的 .ps1 按系统 ANSI 代码页解析，
#       里面的中文会变成乱码；带 BOM 才会按 UTF-8 读取。
#
# ⚠ 若运行时报「禁止运行脚本」(PSSecurityException / UnauthorizedAccess)，
#   那是 Windows 默认的 PowerShell 执行策略在拦，与本项目无关。三种解法：
#     1) 直接用 run_all.bat（推荐，不受执行策略影响）
#     2) powershell -ExecutionPolicy Bypass -File .\run_all.ps1
#     3) Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass（仅当前窗口）
#
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 解释器选择：优先选「真的装了依赖」的那个。
# 本机系统 Python 3.13 缺 duckdb / dotenv，而运行时 Python 有，
# 所以不能用「python 解析到哪个就用哪个」——会静默地在第 6 步炸掉。
$runtime = "C:\Users\Arnold\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$py = $null
if (Test-Path $runtime) {
    & $runtime -c "import duckdb" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $runtime }
}
if (-not $py) {
    $c = Get-Command python -ErrorAction SilentlyContinue
    if ($c) { $py = $c.Source }
}
if (-not $py) { throw "找不到可用的 Python，请先安装依赖" }
& $py -c "import duckdb" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] 该 Python 未安装 duckdb，第 6 步会失败。" -ForegroundColor Yellow
    Write-Host "       安装：& `"$py`" -m pip install duckdb pymysql python-dotenv"
}

Write-Host "=== 银行网点运营指标监控与异常预警 · 一键复现 ===" -ForegroundColor Cyan
Write-Host "Python: $py`n"

Write-Host "[1/8] 环境自检 ..." -ForegroundColor Yellow
& $py src\py\check_env.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "      （有未就绪项，见上方 FAIL/WARN；后续步骤可能失败）" -ForegroundColor Yellow
}
Write-Host ""

$steps = @(
    @{ n = "2/8"; d = "生成模拟数据（含注入自检）";        s = "src\py\gen_operation_data.py" },
    @{ n = "3/8"; d = "三路检测 + 分级 + 成因判定 + 评估";  s = "src\py\analyze_ops.py" },
    @{ n = "4/8"; d = "生成交互式看板";                    s = "src\py\make_dashboard.py" },
    @{ n = "5/8"; d = "生成运营建议报告";                  s = "src\py\gen_report.py" },
    @{ n = "6/8"; d = "落库并真正执行 SQL 指标层";          s = "src\py\load_to_db.py" },
    @{ n = "7/8"; d = "构建单文件网页 Agent（Text-to-Excel）"; s = "src\py\build_web_agent.py" }
)

foreach ($st in $steps) {
    Write-Host "[$($st.n)] $($st.d) ..." -ForegroundColor Yellow
    & $py $st.s
    if ($LASTEXITCODE -ne 0) { throw "$($st.s) 执行失败" }
    Write-Host ""
}

Write-Host "[8/8] 在 Node 沙箱里自检网页 Agent 的 JS ..." -ForegroundColor Yellow
$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
    & node src\py\verify_agent.js
    if ($LASTEXITCODE -ne 0) { throw "网页 Agent 自检未通过" }
} else {
    Write-Host "      （未找到 node，跳过 JS 自检）" -ForegroundColor Yellow
}
Write-Host ""

Write-Host "[*] Markdown 转 Word ..." -ForegroundColor Yellow
# 不传中文文件名参数：控制台代码页转换会破坏参数，
# 交给脚本自己按目录发现文件（见 md2docx.py 的 build_all）。
& $py src\py\md2docx.py --all
if ($LASTEXITCODE -ne 0) { throw "md2docx 执行失败" }

Write-Host "`n=== 全部完成 ===" -ForegroundColor Green
Write-Host "  results\看板.html          （单文件离线可用）"
Write-Host "  results\运营建议报告.docx"
Write-Host "  docs\方法说明书.docx"
Write-Host "  tools\pingan_ops.duckdb    （5 张表已落库）"
