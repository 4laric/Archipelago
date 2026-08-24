import os
import re
import subprocess


os.chdir(os.path.join("..", ".."))
subprocess.run(["py", "-3.13", "Launcher.py", "--", "Build APWorlds"])
subprocess.run(["py", "-3.13", "Launcher.py", "--", "Generate Template Options"])

with open(os.path.join("Players", "Templates", "The Legend of Zelda A Link to the Past.yaml"), "r") as f:
    template_text = f.read()

template_text = re.sub("version: [^ ]+", "version: 0.6.6", template_text)
with open(os.path.join("Players", "Templates", "The Legend of Zelda A Link to the Past.yaml"), "w") as f:
    f.write(template_text)