"""Trusted host launcher: bound durable Docker attach output, no candidate code."""
import os
import resource
import sys

resource.setrlimit(resource.RLIMIT_FSIZE, (64*1024*1024, 64*1024*1024))
os.execv(sys.argv[1], sys.argv[1:])
