# Unshadow-AI Demo
# Run with: vhs demo.tape

Output demo.gif

Set Shell "pwsh"
Set FontSize 14
Set Width 1400
Set Height 800
Set Theme "Dracula"
Set Padding 20
Set TypingSpeed 40ms

# Scene 1: Show banner (no arguments)
Sleep 1s
Type ".\unshadow-ai.exe"
Enter
Sleep 4s

# Scene 2: Run full scan, output JSON to file
Type ".\unshadow-ai.exe -f json -o scan-results.json"
Enter
Sleep 10s

# Scene 3: Show finding counts grouped by application
Type "cat scan-results.json | jq '.extensions | group_by(.application) | map({application: .[0].application, count: length}) | sort_by(-.count)'"
Enter
Sleep 5s
