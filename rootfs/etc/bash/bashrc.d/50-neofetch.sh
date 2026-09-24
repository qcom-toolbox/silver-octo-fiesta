# shellcheck shell=bash
# Greet every new interactive terminal window with neofetch (once per window).
if [[ $- == *i* && -z ${NEOFETCH_SHOWN} && ${TERM} != dumb && -z ${SSH_CONNECTION} ]] \
	&& command -v neofetch >/dev/null 2>&1; then
	export NEOFETCH_SHOWN=1
	neofetch
fi
