' 启动 orch 常驻宿主（无窗口）。
'
' 为什么需要单独一个文件：实测从 Codex / 宿主的命令里 fork 出去的后台进程，
' 会在那条命令结束时被父级 Job Object 一起清掉（CREATE_BREAKAWAY_FROM_JOB 被拒，
' schtasks 在程序黑名单里）。所以宿主必须由一个**不挂在任何对话上**的进程拉起。
'
' 用法：在资源管理器里双击本文件。
'
' 验证状态（2026-11-07）：脚本逻辑与 .vbs 关联（VBSFile → WScript.exe）均正常，
' 但**未能在自动化沙箱内实测**——那里 explorer 与 cscript 都被安全策略拦截。
' 确认可用的是终端方式：
'     cd <orch 仓库目录>
'     python -m orch.dispatch --serve
'
' 启动后自检：python -m orch.dispatch --status

Option Explicit

Dim fso, sh, root, pyw, cmd, home

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)

' 解释器按顺序找：托管 pythonw → 仓库内 .venv → PATH。
' 路径从 %USERPROFILE% 推，不写死用户名——换台机器照样能跑。
home = sh.ExpandEnvironmentStrings("%USERPROFILE%")
pyw = fso.BuildPath(home, ".workbuddy\binaries\python\versions\3.13.12\pythonw.exe")
If Not fso.FileExists(pyw) Then
    If fso.FileExists(root & "\.venv\Scripts\pythonw.exe") Then
        pyw = root & "\.venv\Scripts\pythonw.exe"
    Else
        pyw = "pythonw.exe"
    End If
End If

sh.CurrentDirectory = root
cmd = """" & pyw & """ -m orch.dispatch --serve --runs-dir runs"
sh.Run cmd, 0, False
