import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app import app as application  # replace "app" with your actual filename/module

#wsgi = imp.load_source('wsgi', 'passenger_wsgi.py')
#application = wsgi.app