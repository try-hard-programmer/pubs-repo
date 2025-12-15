"""Database module entry point."""
from src.database.core import DatabaseCore
from src.database.operations import DatabaseOperationsMixin

# Combine Core Infrastructure and Business Operations
class Database(DatabaseCore, DatabaseOperationsMixin):
    pass

# Global database instance
db = Database()