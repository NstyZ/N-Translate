# setup.ps1 -- 在新电脑上一次性配置「N-Translate」
#
# 做四件事：
#   1) 找到 Python 3.10+ 并创建独立虚拟环境 .venv
#   2) 安装经过验证的依赖版本（OpenVINO / optimum-intel / NNCF 等）
#   3) 若 models\ 下缺少模型，则自动下载并量化导出（需要网络）
#   4) 校验 NPU 是否可用，并按需注册「插件自动唤起」与「开机自启」
#
# 用法：双击 setup.bat，或
#   powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
#   powershell ... -File setup.ps1 -Yes            # 全部默认同意，不提问
#   powershell ... -File setup.ps1 -SkipModels     # 只装环境，不导出模型
#   powershell ... -File setup.ps1 -NoRegister     # 只装环境，不注册启动器/自启
param(
    [switch]$Yes,
    [switch]$SkipModels,
    [switch]$NoRegister
)

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location -LiteralPath $root

$venv    = Join-Path $root '.venv'
$vpy     = Join-Path $venv 'Scripts\python.exe'
$ocli    = Join-Path $venv 'Scripts\optimum-cli.exe'
# 只保留全静态变体：NPU 必需，CPU/GPU 也能跑（见 README「为什么只有一种导出」）
$modelsB = Join-Path $root 'models\opus-en-zh-int8-stateless-ov'

function Say($m)  { Write-Host $m }
function Ok($m)   { Write-Host "[OK]  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!]   $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "[ERR] $m" -ForegroundColor Red }

function Ask($question, $defaultYes = $true) {
    if ($Yes) { return $defaultYes }
    $suffix = if ($defaultYes) { '[Y/n]' } else { '[y/N]' }
    $a = Read-Host "$question $suffix"
    if ([string]::IsNullOrWhiteSpace($a)) { return $defaultYes }
    return ($a -match '^(y|yes|Y|是|好|确认)')
}

Say ''
Say '============================================================'
Say '  N-Translate - 环境安装（新电脑一次性配置）'
Say '============================================================'
Say ''

# ---------------------------------------------------------------- 1) Python
function Test-Py([string]$exe, [string[]]$pre) {
    try {
        $out = & $exe @pre --version 2>&1 | Out-String
        if ($out -match 'Python 3\.(\d+)') {
            return ([int]$Matches[1] -ge 10)
        }
    } catch { }
    return $false
}

Say '[1/5] 查找 Python 3.10+ ...'
$py = $null
foreach ($c in @(@('py', '-3'), @('py', '-3.13'), @('python'), @('python3'))) {
    $exe = $c[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $pre = @()
    if ($c.Count -gt 1) { $pre = @($c[1..($c.Count - 1)]) }
    if (Test-Py $exe $pre) { $py = @{ exe = $exe; pre = $pre }; break }
}
if (-not $py) {
    foreach ($p in @(
        (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue),
        (Get-ChildItem 'C:\Python3*\python.exe' -ErrorAction SilentlyContinue),
        (Get-ChildItem 'C:\Program Files\Python3*\python.exe' -ErrorAction SilentlyContinue)
    )) {
        if ($p -and (Test-Py $p.FullName @())) { $py = @{ exe = $p.FullName; pre = @() }; break }
    }
}
if (-not $py) {
    Bad '未找到 Python 3.10 或更高版本。'
    Say '      请到 https://www.python.org/downloads/windows/ 安装（勾选 Add python.exe to PATH），然后重跑 setup.bat。'
    exit 1
}
Ok "使用 Python: $($py.exe) $($py.pre -join ' ')"

# ---------------------------------------------------------------- 2) venv
Say ''
Say '[2/5] 创建虚拟环境 .venv ...'
if (Test-Path -LiteralPath $vpy) {
    Ok '虚拟环境已存在，跳过创建'
} else {
    # 用 --without-pip 创建后再手动引导 pip：比默认方式更稳
    # （默认方式由 ensurepip 在隔离子进程中完成，在受限环境下容易失败）
    & $py.exe @($py.pre) -m venv --without-pip $venv
    if (-not (Test-Path -LiteralPath $vpy)) {
        Bad '创建虚拟环境失败。'
        exit 1
    }
    Ok '虚拟环境已创建'
}

# 确保 venv 内有可用的 pip（新建的 / 之前损坏的都会在这里被修复）
$pipOk = $false
try {
    & $vpy -m pip --version *> $null
    $pipOk = ($LASTEXITCODE -eq 0)
} catch { $pipOk = $false }

if (-not $pipOk) {
    Say '  - 引导安装 pip ...'
    & $vpy -m ensurepip --upgrade --default-pip
    try {
        & $vpy -m pip --version *> $null
        $pipOk = ($LASTEXITCODE -eq 0)
    } catch { $pipOk = $false }
}
if (-not $pipOk) {
    Bad '虚拟环境里没有可用的 pip，Python 安装可能不完整。'
    Say '      请重新安装 Python（勾选 Add python.exe to PATH 与 pip），再重跑 setup.bat。'
    exit 1
}
Ok 'pip 就绪'

# ---------------------------------------------------------------- 3) 依赖
Say ''
Say '[3/5] 安装依赖（首次约 3-8 分钟，需要联网）...'
$env:PYTHONIOENCODING = 'utf-8'

& $vpy -m pip install --upgrade pip setuptools wheel --quiet
Say '  - optimum-intel + openvino + nncf + torch ...'
& $vpy -m pip install "optimum-intel[openvino]==2.1.0"
if ($LASTEXITCODE -ne 0) { Bad '依赖安装失败（网络或代理问题？）'; exit 1 }

# 精确回退到导出器实测可用的版本（避免 optimum 的版本门禁报错）
& $vpy -m pip install --no-deps "transformers==4.57.6" "huggingface_hub==0.36.2"
& $vpy -m pip install sentencepiece sacremoses protobuf accelerate

& $vpy -c "import openvino, optimum, transformers, nncf; print('  versions: openvino', openvino.__version__, '| transformers', transformers.__version__)"
Ok '依赖安装完成'

# ---------------------------------------------------------------- 4) 模型
Say ''
Say '[4/5] 检查模型文件 ...'
$haveModels = (Test-Path -LiteralPath (Join-Path $modelsB 'openvino_decoder_model.xml'))

if ($haveModels) {
    Ok '基础模型已就绪（OPUS-MT，NPU 可直接跑）'
} elseif ($SkipModels) {
    Warn '按参数要求跳过模型下载；服务将无法启动，请稍后运行 install_model.bat'
} else {
    Warn '缺少基础模型，开始下载并导出（约 300MB 下载 + 1-2 分钟量化）...'
    $env:HF_ENDPOINT = 'https://hf-mirror.com'
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
    New-Item -ItemType Directory -Force -Path (Join-Path $root 'models') | Out-Null

    Say '  - 导出无状态全静态版本（NPU / CPU / GPU 通用）...'
    & $ocli export openvino --model Helsinki-NLP/opus-mt-en-zh --weight-format int8 --disable-stateful $modelsB
    if (Test-Path -LiteralPath (Join-Path $modelsB 'openvino_decoder_model.xml')) {
        Ok '基础模型导出完成'
    } else {
        Bad '基础模型导出失败（请检查网络后重跑 setup.bat）'
    }
}

# 主模型：Index-Translate 2B（GGUF，1.2GB）。它是默认模型，没装也能启动，会自动回落。
if ($SkipModels) {
    Warn '按参数要求跳过主模型下载'
} else {
    $gguf = Join-Path $root 'models\index-translate-2b-gguf\Index-Translate-2B.Q4_K_M.gguf'
    if (Test-Path -LiteralPath $gguf) {
        Ok '主模型 Index-Translate 2B 已存在'
    } elseif (Ask '是否下载主模型 Index-Translate 2B（约 1.2GB，中文质量最好）？') {
        & $vpy (Join-Path $root 'install_model.py') --id index-translate-2b
        if (Test-Path -LiteralPath $gguf) { Ok '主模型下载完成' } else { Warn '主模型下载未完成，可稍后运行 install_model.bat index-translate-2b' }
    }
}

# llama.cpp 运行时：Index-Translate 走它（CPU 版约 24MB，NVIDIA 显卡可选 CUDA 版）
$llamaExe = Join-Path $root 'llama\llama-server.exe'
if (Test-Path -LiteralPath $llamaExe) {
    Ok 'llama.cpp 运行时已存在'
} elseif ($SkipModels) {
    Warn '按参数要求跳过 llama.cpp 下载（需要时运行 install_llama.bat）'
} else {
    Say '  - 下载 llama.cpp 运行时（CPU 版，约 24MB）...'
    & $vpy (Join-Path $root 'install_llama.py') --flavor cpu
    if (Test-Path -LiteralPath $llamaExe) { Ok 'llama.cpp 就绪' } else { Warn 'llama.cpp 下载失败，可稍后运行 install_llama.bat' }
}

# ---------------------------------------------------------------- 5) 自检
Say ''
Say '[5/5] 自检 ...'
& $vpy (Join-Path $root 'npu_sanity.py')
if ($LASTEXITCODE -ne 0) {
    Warn 'NPU 自检未通过：可能是未安装/未更新 Intel NPU 驱动。'
    Say  '      驱动下载：https://www.intel.com/content/www/us/en/download/794734/intel-npu-driver-windows.html'
}

# ---------------------------------------------------------------- 注册
Say ''
if ($NoRegister) {
    Warn '按参数要求跳过注册步骤（可稍后手动运行 fix_extension.py / install_autostart.bat）'
} else {
    # fix_extension.py 会：确保 manifest 无 key（有 key 会导致 Chrome 重启时卸载插件）、
    # 按本机安装目录算出正确的扩展 ID 并完成 Native Messaging 宿主注册。
    if (Ask '是否注册「插件自动唤起本地服务」（推荐，需一次注册）？' $true) {
        & $vpy (Join-Path $root 'fix_extension.py')
    }
    Say ''
    if (Ask '是否开启「开机自动启动本地服务」（之后完全无需手动启动）？' $true) {
        powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'install_autostart.ps1')
    }
}

# ---------------------------------------------------------------- 完成
Say ''
Say '============================================================'
Say '  安装完成 ✅'
Say '============================================================'
Say ''
Say '接下来（在 Chrome 里，只需做一次）：'
Say '  1) 打开 chrome://extensions ，开启右上角「开发者模式」'
Say "  2) 点「加载已解压的扩展程序」，选择目录：$root\chrome-extension"
Say '  3) 打开任意英文网页，右键 →「N-Translate：翻译整个页面」'
Say ''
Say '注：扩展 ID 由安装目录决定。若你把 chrome-extension 换到别的目录，请重跑一次'
Say '    fix_extension.py 重新注册（它会按新路径重算 ID）。'
Say ''
Say '启动/停止服务：双击 start_server.bat / stop_server.bat'
Say '详细说明见：README.md 与 chrome-extension\INSTALL.md'
Say ''
