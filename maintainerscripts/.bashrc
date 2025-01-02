set -o vi
bind '"\e[A": history-search-backward'
bind '"\e[B": history-search-forward'
# readline variables and bindings
bind 'set keymap vi'
bind 'set editing-mode vi'
bind 'set mark-directories on'
bind 'set mark-symlinked-directories on'
bind 'set page-completions off'
bind 'set show-all-if-ambiguous on'
bind 'set visible-stats on'
bind 'set completion-query-items 9001'
set show-mode-in-prompt on
set vi-cmd-mode-string "\1\e[2 q\2"
set vi-ins-mode-string "\1\e[6 q\2"
set show-mode-in-prompt on
set vi-cmd-mode-string "\1\e[2 q\2cmd"
set vi-ins-mode-string "\1\e[6 q\2ins"

export HISTCONTROL=ignorespace
source /usr/share/doc/pkgfile/command-not-found.bash
shopt -s checkwinsize
export WORKON_HOME=~/.virtualenvs
export DOCKER_HOST="unix:$XDG_RUNTIME_DIR/podman/podman.sock"
source /usr/bin/virtualenvwrapper_lazy.sh
/usr/bin/aureport -n --start this-week
echo ""

# Added by LM Studio CLI (lms)
export PATH="$PATH:/home/evo/.cache/lm-studio/bin"

export PATH="$PATH:/home/evo/scripts"

# pnpm
export PNPM_HOME="/home/evo/.local/share/pnpm"
case ":$PATH:" in
*":$PNPM_HOME:"*) ;;
*) export PATH="$PNPM_HOME:$PATH" ;;
esac
# pnpm end
source /usr/share/nvm/init-nvm.sh

#alioases
function aurclone() {
  echo "function call"
  cd "$(python ~/scripts/aurclone $1)"
}

alias aur="cd ~/github/envolution/aur/maintain/build/"

# bun
export BUN_INSTALL="$HOME/.bun"
export PATH="$BUN_INSTALL/bin:$PATH"
