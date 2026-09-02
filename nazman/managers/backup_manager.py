from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from pathlib import Path
import shutil
import shlex
from sqlalchemy.orm import Session

from ..models.backup import BackupCommit
from ..config import get_settings
from ..utils.exceptions import BackupError


class BackupManager:
    """Manages Git-based configuration backup."""
    
    def __init__(self):
        self.settings = get_settings()
        self.repo_path = Path(self.settings.backup_repo_path)
    
    async def initialize_backup_repo(self) -> None:
        """Initialize the backup repository."""
        try:
            # Create backup directory if it doesn't exist
            self.repo_path.mkdir(parents=True, exist_ok=True)
            
            # Initialize git repo if not already initialized
            git_dir = self.repo_path / ".git"
            if not git_dir.exists():
                await self._run_git("init")
                await self._run_git("config", "user.email", "nazman@localhost")
                await self._run_git("config", "user.name", "NAZMan")
                
                # Create initial commit
                readme_path = self.repo_path / "README.md"
                readme_path.write_text("# NAZMan Configuration Backup\n\nThis repository contains NAZMan configuration backups.\n")
                
                await self._run_git("add", "README.md")
                await self._run_git("commit", "-m", "Initial backup repository")
            
        except Exception as e:
            raise BackupError(f"Failed to initialize backup repository: {str(e)}")
    
    async def backup_configuration(self, db: Session, message: Optional[str] = None) -> BackupCommit:
        """Backup current configuration to Git repository."""
        try:
            # Ensure repo is initialized
            await self.initialize_backup_repo()
            
            # Copy database to backup location (consistent snapshot via sqlite3
            # .backup — a raw shutil.copy2 of a WAL-mode DB can be torn).
            db_path = Path(self.settings.database_path)
            backup_db_path = self.repo_path / "nazman.db"
            backup_db_path.parent.mkdir(parents=True, exist_ok=True)
            if db_path.exists():
                _, _, rc = await self._run_command(
                    ["sqlite3", str(db_path), shlex.quote(f".backup {str(backup_db_path)}")]
                )
                if rc != 0:
                    shutil.copy2(db_path, backup_db_path)
            
            # Copy system configuration files
            config_files = [
                "/etc/exports",
                "/etc/default/nfs-kernel-server"
            ]
            
            config_dir = self.repo_path / "system-config"
            config_dir.mkdir(exist_ok=True)
            
            for config_file in config_files:
                if Path(config_file).exists():
                    dest_path = config_dir / Path(config_file).name
                    shutil.copy2(config_file, dest_path)
            
            # Export pool configurations
            await self._export_pool_configs()
            
            # Export partition tables
            await self._export_partition_tables()
            
            # Stage all changes
            await self._run_git("add", "-A")
            
            # Check if there are changes to commit
            stdout, stderr, returncode = await self._run_git("status", "--porcelain")
            
            if not stdout.strip():
                # No changes to commit
                return BackupCommit(
                    commit_hash="none",
                    commit_message="No changes to backup",
                    files_changed=0
                )
            
            # Create commit
            commit_message = message or f"Configuration backup - {datetime.now(timezone.utc).isoformat()}"
            await self._run_git("commit", "-m", commit_message)
            
            # Get commit hash
            stdout, stderr, returncode = await self._run_git("rev-parse", "HEAD")
            commit_hash = stdout.strip()
            
            # Push if configured
            if self.settings.backup_push_on_commit:
                await self._push_backup()
            
            # Record in database
            backup_commit = BackupCommit(
                commit_hash=commit_hash,
                commit_message=commit_message,
                files_changed=len(stdout.strip().split('\n')) if stdout.strip() else 0
            )
            db.add(backup_commit)
            db.commit()
            db.refresh(backup_commit)
            
            return backup_commit
            
        except Exception as e:
            if isinstance(e, BackupError):
                raise
            raise BackupError(f"Failed to backup configuration: {str(e)}")
    
    async def restore_configuration(
        self, 
        db: Session, 
        commit_hash: str
    ) -> bool:
        """Restore configuration from a specific commit."""
        try:
            # Verify commit exists
            stdout, stderr, returncode = await self._run_git(
                "rev-parse", "--verify", commit_hash
            )
            
            if returncode != 0:
                raise BackupError(f"Commit {commit_hash} not found")
            
            # Checkout the commit
            await self._run_git("checkout", commit_hash, "--", ".")
            
            # Restore database
            backup_db_path = self.repo_path / "nazman.db"
            if backup_db_path.exists():
                db_path = Path(self.settings.database_path)
                shutil.copy2(backup_db_path, db_path)
            
            # Restore system configuration files
            config_dir = self.repo_path / "system-config"
            if config_dir.exists():
                for config_file in config_dir.iterdir():
                    if config_file.is_file():
                        dest_path = f"/etc/{config_file.name}"
                        shutil.copy2(config_file, dest_path)
            
            # Apply NFS exports
            exports_file = config_dir / "exports"
            if exports_file.exists():
                await self._apply_exports(exports_file)
            
            return True
            
        except Exception as e:
            if isinstance(e, BackupError):
                raise
            raise BackupError(f"Failed to restore configuration: {str(e)}")
    
    async def get_backup_history(self, db: Session, limit: int = 50) -> List[BackupCommit]:
        """Get backup commit history."""
        return db.query(BackupCommit)\
            .order_by(BackupCommit.created_at.desc())\
            .limit(limit)\
            .all()
    
    async def get_backup_status(self) -> Dict[str, Any]:
        """Get backup system status."""
        try:
            # Check if repo exists
            git_dir = self.repo_path / ".git"
            repo_exists = git_dir.exists()
            
            # Get last commit
            last_commit = None
            if repo_exists:
                stdout, stderr, returncode = await self._run_git(
                    "log", "-1", "--format=%H %ci %s"
                )
                if returncode == 0 and stdout.strip():
                    parts = stdout.strip().split(' ', 2)
                    if len(parts) >= 3:
                        last_commit = {
                            "hash": parts[0],
                            "date": parts[1],
                            "message": parts[2]
                        }
            
            # Check if there are uncommitted changes
            has_changes = False
            if repo_exists:
                stdout, stderr, returncode = await self._run_git("status", "--porcelain")
                has_changes = bool(stdout.strip())
            
            return {
                "repo_exists": repo_exists,
                "repo_path": str(self.repo_path),
                "last_commit": last_commit,
                "has_uncommitted_changes": has_changes,
                "backup_enabled": self.settings.backup_enabled
            }
            
        except Exception as e:
            return {
                "repo_exists": False,
                "error": str(e),
                "backup_enabled": self.settings.backup_enabled
            }
    
    async def _export_pool_configs(self) -> None:
        """Export ZFS pool configurations."""
        try:
            # Export pool list
            stdout, stderr, returncode = await self._run_command(
                ["zpool", "list", "-H", "-o", "name"]
            )
            
            if returncode == 0:
                pools = stdout.strip().split('\n')
                
                config_dir = self.repo_path / "pool-configs"
                config_dir.mkdir(exist_ok=True)
                
                for pool_name in pools:
                    if pool_name:
                        # Export pool status
                        stdout, stderr, returncode = await self._run_command(
                            ["zpool", "status", "-j", pool_name]
                        )
                        
                        if returncode == 0:
                            pool_config_path = config_dir / f"{pool_name}.json"
                            pool_config_path.write_text(stdout)
                        
                        # Export pool properties
                        stdout, stderr, returncode = await self._run_command(
                            ["zpool", "get", "-j", "all", pool_name]
                        )
                        
                        if returncode == 0:
                            pool_props_path = config_dir / f"{pool_name}-props.json"
                            pool_props_path.write_text(stdout)
            
        except Exception as e:
            # Log error but don't fail backup
            print(f"Warning: Failed to export pool configs: {str(e)}")
    
    async def _export_partition_tables(self) -> None:
        """Export partition tables for all disks."""
        try:
            # Get list of disks
            stdout, stderr, returncode = await self._run_command(
                ["lsblk", "-d", "-n", "-o", "NAME"]
            )
            
            if returncode == 0:
                disks = stdout.strip().split('\n')
                
                config_dir = self.repo_path / "partition-tables"
                config_dir.mkdir(exist_ok=True)
                
                for disk in disks:
                    disk = disk.strip()
                    if disk:
                        # Export partition table using sfdisk
                        stdout, stderr, returncode = await self._run_command(
                            ["sfdisk", "-d", f"/dev/{disk}"]
                        )
                        
                        if returncode == 0:
                            partition_path = config_dir / f"{disk}.sfdisk"
                            partition_path.write_text(stdout)
            
        except Exception as e:
            # Log error but don't fail backup
            print(f"Warning: Failed to export partition tables: {str(e)}")
    
    async def _apply_exports(self, exports_file: Path) -> None:
        """Apply NFS exports from file."""
        try:
            # Copy exports file
            shutil.copy2(exports_file, "/etc/exports")
            
            # Reload exports
            await self._run_command(["exportfs", "-ra"], op="system", category="nfs")
            
        except Exception as e:
            raise BackupError(f"Failed to apply exports: {str(e)}")
    
    async def _push_backup(self) -> None:
        """Push backup to remote repository."""
        # For now, this is a local-only backup
        # Could be extended to push to remote Git repos
        pass
    
    async def _run_git(self, *args) -> tuple:
        """Run a git command (tagged write/backup unless a read-only subcommand)."""
        cmd = ["git", "-C", str(self.repo_path)] + list(args)
        sub = str(args[0]) if args else ""
        if sub in ("status", "rev-parse", "log", "show", "diff", "ls-files"):
            return await self._run_command(cmd, op="read", category="backup")
        return await self._run_command(cmd, op="write", category="backup")
    
    async def _run_command(self, cmd: list, **kwargs) -> tuple:
        """Run a system command (via run_command so it is captured in the command log)."""
        from ..utils.commands import run_command
        try:
            return await run_command(cmd, timeout=60, check=False, **kwargs)
        except Exception as e:
            return ("", str(e), -1)


# Singleton instance
backup_manager = BackupManager()
