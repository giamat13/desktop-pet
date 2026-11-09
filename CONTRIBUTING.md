# Contributing

Thanks for helping improve Desktop Pet.

## Before opening a pull request

- Keep changes focused and follow the existing Python, PowerShell, and
  JavaScript patterns.
- Run the Windows self-checks:

  ```powershell
  py -3 -m pip install -r requirements.txt
  py -3 test_pet_app.py
  ```

- Describe the behavior changed and how it was tested.
- Do not commit credentials, private keys, local settings, or personal logs.
- If a change affects an integration, document its permissions, network
  activity, and setup steps.

## Reporting issues

Include your Windows version, Python version, steps to reproduce, and relevant
error messages. Remove account names, tokens, and other personal information
from logs before sharing them.
