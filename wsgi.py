import sys

# Point to the folder with your project files on PythonAnywhere
path = '/home/joyal'
if path not in sys.path:
    sys.path.insert(0, path)

from app import app as application
