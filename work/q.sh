#!/bin/bash
# Run SQL on the devbox: query text arrives on stdin.
: "${SME_DB_HOST:?export SME_DB_HOST to the devbox host running smeassist MySQL}"
exec ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "root@$SME_DB_HOST" 'mysql smeassist -t'
