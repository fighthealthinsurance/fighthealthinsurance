--# Development Notes

## Accessing Ray Dashboard

The Ray dashboard provides monitoring for distributed task execution, logs, and metrics.

### Port Forward to Ray Service

```bash
kubectl port-forward -n totallylegitco service/raycluster-kuberay-head-svc 8265:8265
```

### Access Dashboard

Open in your browser: http://localhost:8265

The port forward will continue running in your terminal. Press `Ctrl+C` to stop it when done.

### What You Can See

- Task execution logs (including fax logging improvements)
- Distributed system metrics
- Ray actor states
- Resource utilization
