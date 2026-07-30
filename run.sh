#!/usr/bin/env bash
# Manage the single local Telegram Media Archiver deployment and its data volume.
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
calling_dir=$(pwd -P)
command_name=""
data_argument="$script_dir/data"
log_level="INFO"

usage() {
  cat <<'EOF'
Usage: ./run.sh [-d|--data-dir DATA_DIR] [-l|--log-level LEVEL] <command>

Commands:
  start     Build the current image and start the service in the background.
  restart   Build the current image and recreate the running service.
  stop      Stop the service while retaining its container and all data.
  down      Remove the service container and network while retaining all data.

Options:
  -d, --data-dir PATH  Host directory for SQLite, sessions, downloads, and thumbnails.
                        Defaults to <repository>/data. Relative paths are resolved from
                        the directory where this command is run.
  -l, --log-level LEVEL
                        Application log level: DEBUG, INFO, WARNING, ERROR, or CRITICAL.
                        Defaults to INFO.
  -h, --help            Show this help text.

Examples:
  ./run.sh start
  ./run.sh -d /mnt/nas/telegram-archive start
  ./run.sh -d ./data restart
  ./run.sh --log-level DEBUG restart
  ./run.sh -d ./data down
EOF
}

while (($#)); do
  case "$1" in
    -d|--data-dir)
      if (($# < 2)) || [[ -z "${2:-}" ]]; then
        printf 'Error: %s requires a directory path.\n' "$1" >&2
        exit 2
      fi
      data_argument=$2
      shift 2
      ;;
    -l|--log-level)
      if (($# < 2)) || [[ -z "${2:-}" ]]; then
        printf 'Error: %s requires a log level.\n' "$1" >&2
        exit 2
      fi
      log_level=${2^^}
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    start|restart|stop|down)
      if [[ -n "$command_name" ]]; then
        printf 'Error: only one command may be supplied.\n' >&2
        exit 2
      fi
      command_name=$1
      shift
      ;;
    *)
      printf 'Error: unsupported command or option %q.\n\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$command_name" ]]; then
  printf 'Error: a command is required.\n\n' >&2
  usage >&2
  exit 2
fi

case "$log_level" in
  DEBUG|INFO|WARNING|ERROR|CRITICAL) ;;
  *)
    printf 'Error: unsupported log level %q (expected DEBUG, INFO, WARNING, ERROR, or CRITICAL).\n' "$log_level" >&2
    exit 2
    ;;
esac

if [[ "$data_argument" != /* ]]; then
  data_argument="$calling_dir/$data_argument"
fi
data_dir=$(realpath -m -- "$data_argument")

if [[ "$data_dir" == "/" || "$data_dir" == "$script_dir" ]]; then
  printf 'Error: refusing to use %q as the service data directory.\n' "$data_dir" >&2
  exit 2
fi

if ! docker compose version >/dev/null 2>&1; then
  printf 'Error: Docker Compose v2 is required (expected: docker compose).\n' >&2
  exit 1
fi

if [[ -e "$data_dir" && ! -d "$data_dir" ]]; then
  printf 'Error: data path is not a directory: %s\n' "$data_dir" >&2
  exit 1
fi

if [[ "$command_name" == "start" || "$command_name" == "restart" ]]; then
  mkdir -p -- "$data_dir"
elif [[ ! -e "$data_dir" ]]; then
  printf 'Error: data directory does not exist: %s\n' "$data_dir" >&2
  printf 'Refusing to %s because the path may be misspelled.\n' "$command_name" >&2
  exit 1
fi

if [[ ! -d "$data_dir" ]]; then
  printf 'Error: data path is not a directory: %s\n' "$data_dir" >&2
  exit 1
fi

export ARCHIVER_DATA_DIR="$data_dir"
export APP_UID="$(id -u)"
export APP_GID="$(id -g)"
export LOG_LEVEL="$log_level"

compose=(docker compose --project-directory "$script_dir" --file "$script_dir/docker-compose.yml")

printf 'Using data directory: %s\n' "$ARCHIVER_DATA_DIR"
printf 'Using application log level: %s\n' "$LOG_LEVEL"

follow_logs() {
  printf 'Following service logs (press Ctrl+C to stop viewing logs; the service stays running).\n'
  set +e
  "${compose[@]}" logs --follow --tail=100
  log_status=$?
  set -e

  if [[ "$log_status" -ne 0 && "$log_status" -ne 130 ]]; then
    return "$log_status"
  fi
  printf '\nStopped following logs. The service is still running.\n'
}

case "$command_name" in
  start)
    "${compose[@]}" up --detach --build
    follow_logs
    ;;
  restart)
    "${compose[@]}" up --detach --build --force-recreate
    follow_logs
    ;;
  stop)
    "${compose[@]}" stop
    ;;
  down)
    "${compose[@]}" down
    printf 'Containers and network removed. Data retained at: %s\n' "$ARCHIVER_DATA_DIR"
    ;;
esac
