## Delivery
Write only the registered output files. Write each file to a temporary file in its destination directory, close it and atomically rename it to the final filename. Standard output and standard error are diagnostic only.

## Ending
Wait for all tools to finish, then exit. When a result cannot be delivered, report the actual failure through the stage's existing result or diagnostic path. Do not fabricate a successful artifact.
