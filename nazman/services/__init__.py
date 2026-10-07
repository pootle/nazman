"""Application services that compose multiple managers.

Managers own one domain each; cross-domain orchestration (e.g. destroying a
pool, which touches ZFS, NFS and SMB) lives here so managers never import
each other.
"""
