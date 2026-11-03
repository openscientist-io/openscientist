"""The container launcher: the only holder of the Docker socket.

Every container it creates comes from one of two server-side templates and
carries its marker label; anything else answers 404. Its config is its env.
"""
