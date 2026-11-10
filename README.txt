SHULEHUB CONTROL - setup
========================
What it is: your private dashboard. Two jobs:
  1. Tracked links: send people a link instead of a file; see who opened it, from how many devices, and whether they downloaded.
  2. Schools: give each school a licence key; ShuleHub checks in (school name, key, version, computer type, a random install id)
     and you see who is active, which version they run, and whether a key is used on several computers.
No student, fee, exam or other school records are ever sent here.

TRY IT ON YOUR OWN COMPUTER
  pip install -r requirements.txt
  set SHUBCTL_PASSWORD=choose-a-password          (Windows)     export SHUBCTL_PASSWORD=...  (Mac/Linux)
  python app.py                                   then open http://127.0.0.1:8000

PUT IT ONLINE (so links work for other people): PythonAnywhere free account (keeps your files and database)
  1. Upload this folder (Files tab).  2. Web tab > Add a new web app > Flask > point it at app.py.
  3. In the WSGI file: from app import app as application
  4. Web tab > Environment variables:  SHUBCTL_PASSWORD = your password   (optional: SHUBCTL_TZ_HOURS = 3)
  5. Reload.  Your links look like  https://YOURNAME.pythonanywhere.com/d/xxxx
Use HTTPS only. Back up control.db now and then.

THEN CONNECT SHULEHUB
  In your ShuleHub folder open checkin_config.py and put this address in SERVER_URL, then rebuild (build_windows.bat).
  For each school: Schools > New school licence > give them the key. They type it in under Menu > Licence & Check-in.

BE OPEN WITH YOUR CLIENTS
  Say in your agreement that ShuleHub sends a licence check-in (school name, key, version, computer type) and that
  tracked download links record when they are opened. ShuleHub also shows schools exactly what is sent
  (Menu > Licence & Check-in). Link previews (WhatsApp, Telegram, email scanners) are recognised and not counted
  as someone opening it.
