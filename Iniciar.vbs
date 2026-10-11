Option Explicit
Dim shell, fso, folder, python, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
python = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python314\pythonw.exe"
If Not fso.FileExists(python) Then
    MsgBox "Python nao encontrado. Verifique as dependencias no LEIA-ME.md.", 16, "Prepara Video TV"
    WScript.Quit 1
End If
shell.CurrentDirectory = folder
command = Chr(34) & python & Chr(34) & " " & Chr(34) & folder & "\app.py" & Chr(34)
shell.Run command, 0, False
