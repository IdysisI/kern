import os
import tempfile

os.environ['KERN_HOME'] = tempfile.mkdtemp(prefix='kern-tests-')
os.environ['KERN_LOCAL'] = '1'
os.environ['KERN_SANDBOX'] = '0'
