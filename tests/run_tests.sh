#!/usr/bin/env bash
# Run every self-test that is safe next to the live services.
#
# Nothing here writes Karing's configuration or the reconciler's state file: the
# tests that need to (an end-to-end fast-path check, which must write
# service_core.json the way the app does) are run by hand instead -- see the README.
set -u

here=$(cd "$(dirname "$0")" && pwd)
failed=()

for test in test_port_parse.py test_reload_budget.py test_resolve.py test_watcher.py test_probe.py test_process_identity.py test_log_sink.py test_gc.py test_tun_watch.py count_core_writes.py; do
    printf '\n===== %s\n' "$test"
    if ! python3 "$here/$test"; then
        failed+=("$test")
    fi
done

printf '\n===== 总结\n'
if [ ${#failed[@]} -eq 0 ]; then
    echo "全部通过"
    exit 0
fi
printf '失败: %s\n' "${failed[*]}"
exit 1
