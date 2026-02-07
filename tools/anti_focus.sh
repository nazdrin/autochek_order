#!/usr/bin/env bash
# Возвращает фокус на последнее НЕ-Chrome приложение, если Chrome всплыл наверх.

osascript <<'APPLESCRIPT'
tell application "System Events"
  set lastNonChrome to ""
  repeat
    try
      set frontApp to name of first application process whose frontmost is true
      if frontApp is not "Google Chrome" then
        set lastNonChrome to frontApp
      else
        if lastNonChrome is not "" then
          set frontmost of application process lastNonChrome to true
        end if
      end if
    end try
    delay 0.2
  end repeat
end tell
APPLESCRIPT