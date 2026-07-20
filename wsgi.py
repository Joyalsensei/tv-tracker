import sys

# Point to the folder with your project files on PythonAnywhere
# If you cloned the repo, it should be at /home/joyal/tv-tracker
# If you placed files directly in /home/joyal/, change to path = '/home/joyal'
path = '/home/joyal/tv-tracker'
if path not in sys.path:
    sys.path.insert(0, path)

from app import app as application
