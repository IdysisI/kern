
import os
os.environ["KERN_AUTO_APPROVE"] = "1"
from kern.tui import ToolCard

c = ToolCard("exec", {"cmd": "ls"})
print("visual after init:", type(c.visual))
c.set_result("exit=0\nhello")
print("visual after set_result:", type(c.visual))
c2 = ToolCard("write", {"path": "x.py", "content": "a"})
c2.set_result("wrote x.py")
c2.set_diff("--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+a")
print("visual with diff:", type(c2.visual))
