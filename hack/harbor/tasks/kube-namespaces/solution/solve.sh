#!/bin/bash
kubectl get namespaces -o name | sed 's#namespace/##' > /app/answer.md
