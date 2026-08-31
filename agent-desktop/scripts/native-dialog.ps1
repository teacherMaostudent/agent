<# Windows 原生文件选择器测试。只操作指定测试进程，绝不按窗口标题操作用户实例。 #>
param([int]$TargetProcessId, [ValidateSet('Inspect','Cancel','Choose')][string]$Action = 'Inspect',
      [string]$TargetPath = '')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$ae = [System.Windows.Automation.AutomationElement]
$scope = [System.Windows.Automation.TreeScope]
Add-Type @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class DesktopTestDialog {
    public delegate bool EnumProc(IntPtr window, IntPtr parameter);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr parameter);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr window, StringBuilder name, int count);
    public static IntPtr Find(int process) {
        IntPtr found = IntPtr.Zero;
        EnumWindows((window, unused) => {
            uint actual; GetWindowThreadProcessId(window, out actual);
            if (actual != process) return true;
            var name = new StringBuilder(128); GetClassName(window, name, name.Capacity);
            if (name.ToString() != "#32770") return true;
            found = window; return false;
        }, IntPtr.Zero);
        return found;
    }
}
'@
$deadline = [DateTime]::UtcNow.AddSeconds(20)
$dialog = $null
do {
    # Owned modal windows may not be direct children in UIA's logical tree.
    # Resolve the native HWND for this exact process, then obtain its UIA element.
    $handle = [DesktopTestDialog]::Find($TargetProcessId)
    if ($handle -ne [IntPtr]::Zero) { $dialog = $ae::FromHandle($handle) }
    if (-not $dialog) { Start-Sleep -Milliseconds 200 }
} while (-not $dialog -and [DateTime]::UtcNow -lt $deadline)
if (-not $dialog) { throw 'Native dialog was not found for the isolated Desktop process.' }
$controls = $dialog.FindAll($scope::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
if ($Action -eq 'Inspect') {
    @($controls | Where-Object { $_.Current.ControlType.ProgrammaticName -in @('ControlType.Edit','ControlType.Button') } |
        ForEach-Object { @{ type=$_.Current.ControlType.ProgrammaticName; name=$_.Current.Name; id=$_.Current.AutomationId } }) | ConvertTo-Json
    exit 0
}
if ($Action -eq 'Cancel') {
    $cancel = $dialog.FindFirst($scope::Descendants,
        [System.Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty, '2'))
    $cancel.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    exit 0
}
# 选择/导出仅允许写入由本回归创建的临时目录，不接受其他工作区或用户文件。
$full = [System.IO.Path]::GetFullPath($TargetPath)
$temp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
if (-not $full.StartsWith($temp, [System.StringComparison]::OrdinalIgnoreCase) -or
    ($full.Substring($temp.Length) -notmatch '^agent-desktop-e2e-[^\\/]+[\\/]')) {
    throw 'TargetPath must remain inside the isolated Desktop test directory.'
}
$edit = $dialog.FindFirst($scope::Descendants,
    [System.Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty, '1001'))
if (-not $edit) { throw 'Native dialog filename/folder edit was not found.' }
$edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($full)
$ok = $dialog.FindFirst($scope::Descendants,
    [System.Windows.Automation.PropertyCondition]::new($ae::AutomationIdProperty, '1'))
$ok.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
