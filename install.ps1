# ============================================================
# memory-whale 一行命令安装脚本
# 用法：
#   1. 把整个仓库放到任意目录（路径随意，脚本不挑地方）
#   2. PowerShell 里进入本目录，运行：
#        .\install.ps1                          -> 会问你 AstrBot 根目录在哪
#        .\install.ps1 -AstrBotRoot "C:\AstrBot" -> 直接装
#   脚本只做拷贝，不改系统、不联网、不写入任何密钥。
# ============================================================
param(
    [string]$AstrBotRoot = ""
)

$ErrorActionPreference = "Stop"

# --- 0. 定位自己（仓库根 = 脚本所在目录）---
$RepoRoot = $PSScriptRoot
if (-not $RepoRoot) {
    Write-Host "[x] 请在 PowerShell 里用 .\install.ps1 运行本脚本，不要复制粘贴进别的解释器。" -ForegroundColor Red
    exit 1
}

$SrcDir = Join-Path $RepoRoot "src"
if (-not (Test-Path $SrcDir)) {
    Write-Host "[x] 没找到 $SrcDir ，仓库不完整，请重新下载完整仓库。" -ForegroundColor Red
    exit 1
}

# --- 1. 找 AstrBot 根目录 ---
if (-not $AstrBotRoot) {
    Write-Host "请输入你的 AstrBot 根目录（就是 data 文件夹的上一层，例如 C:\AstrBot）："
    $AstrBotRoot = (Read-Host).Trim('"').Trim()
}
$AstrBotRoot = $AstrBotRoot.TrimEnd('\')

$PluginsDir = Join-Path $AstrBotRoot "data\plugins"
if (-not (Test-Path $PluginsDir)) {
    Write-Host "[x] $PluginsDir 不存在，这个目录不像 AstrBot 根目录。" -ForegroundColor Red
    Write-Host "    提示：data 文件夹里应该有 plugins、config 等子目录，确认后重跑。" -ForegroundColor Yellow
    exit 1
}

# --- 2. 目标插件目录 ---
$Target = Join-Path $PluginsDir "astrbot_plugin_memory_whale"

if (Test-Path $Target) {
    Write-Host "[!] 已存在 $Target" -ForegroundColor Yellow
    $ans = Read-Host "    覆盖安装？(y/N)"
    if ($ans -notin @("y", "Y")) {
        Write-Host "已取消，未做任何改动。"
        exit 0
    }
    # 覆盖前备份旧的 data/（用户攒的本子在这里，绝不能丢）
    $OldUserData = Join-Path $Target "data"
    if (Test-Path $OldUserData) {
        $Backup = "$Target`_data_backup_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
        Copy-Item $OldUserData $Backup -Recurse
        Write-Host "[i] 你的记忆本子已备份到：$Backup" -ForegroundColor Green
    }
    Remove-Item $Target -Recurse -Force
}

# --- 3. 拷贝插件本体 ---
New-Item -ItemType Directory -Path $Target | Out-Null
Copy-Item (Join-Path $SrcDir "*") $Target -Recurse -Force

# --- 4. 核对关键文件 ---
$Meta = Join-Path $Target "metadata.yaml"
$Main = Join-Path $Target "main.py"
if (-not (Test-Path $Meta) -or -not (Test-Path $Main)) {
    Write-Host "[x] 装完发现缺 metadata.yaml 或 main.py，仓库可能不完整。" -ForegroundColor Red
    Write-Host "    已把文件留在 $Target ，请检查后手动处理。"
    exit 1
}

# --- 5. 完成，指路下一步 ---
Write-Host ""
Write-Host "[ok] 装好了：$Target" -ForegroundColor Green
Write-Host ""
Write-Host "接下来三步：" -ForegroundColor Cyan
Write-Host "  1. 打开 AstrBot WebUI -> 插件管理，找到 astrbot_plugin_memory_whale，点【重载】"
Write-Host "     （如果你的 AstrBot 版本没有重载按钮，重启 AstrBot 也一样）"
Write-Host "  2. 想让它压话，二选一："
Write-Host "     - 本地免费：装 ollama 并 ollama pull qwen2.5:7b"
Write-Host "     - 云端快：在插件面板填一把便宜档的 key"
Write-Host "  3. 随便聊两句，然后发 /m3_dump 看本子收到了多少"
Write-Host ""
Write-Host "没配模型也不崩：它会收话、攒字，只是压不动，并老实告诉你拿不到模型。"
