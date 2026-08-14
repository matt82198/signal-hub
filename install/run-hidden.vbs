' run-hidden.vbs -- VBScript launcher for the signal-hub Windows Scheduled Task.
'
' Copied from the aesop watchdog family (aesop/daemons/run-hidden.vbs) with one
' addition: an optional leading "--cwd <dir>" pair, consumed here and applied as
' the child's working directory. signal-hub needs it because "python -m
' signal_hub" only resolves the package when the repo root is the working
' directory, and a scheduled task starts wherever Windows feels like.
'
' Usage:
'   wscript.exe //B //Nologo run-hidden.vbs [--cwd <dir>] <exe> <arg> ...
'
' Window style 0 (hidden) is the reason this file exists at all: a raw exe
' action flashes a console every interval. shell.Run WAITS for the child, so
' the task instance lives as long as the work, which is what makes
' MultipleInstances IgnoreNew, ExecutionTimeLimit and LastTaskResult mean
' anything.
'
' By contract, arguments never contain double quotes.

Dim shell, cmd, i, first, arg
Dim windowStyle, rc

Set shell = CreateObject("WScript.Shell")

first = 0

' Optional --cwd <dir>: consume the pair before building the command line.
If WScript.Arguments.Count >= 2 Then
    If WScript.Arguments(0) = "--cwd" Then
        shell.CurrentDirectory = WScript.Arguments(1)
        first = 2
    End If
End If

cmd = ""
For i = first To WScript.Arguments.Count - 1
    arg = WScript.Arguments(i)
    If i > first Then cmd = cmd & " "
    ' Arguments never contain quotes by contract; wrap in quotes for safety.
    cmd = cmd & """" & arg & """"
Next

' Window style 0 = hidden; True = wait for the child to exit.
windowStyle = 0

rc = shell.Run(cmd, windowStyle, True)

' Exit with the child's exit code, never a blanket 0.
WScript.Quit rc
