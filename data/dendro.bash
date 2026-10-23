# SPDX-License-Identifier: GPL-3.0-or-later
# Bash completion for the dendro command.

_dendro_completion()
{
    local cur word
    local -i has_file_argument=0 options_ended=0 index
    local options='-h --help -v --version -d --debug'

    cur=${COMP_WORDS[COMP_CWORD]}
    COMPREPLY=()

    # Dendro accepts at most one positional argument: a local RPM file.
    # Do not offer another file once one has already been supplied. Respect
    # an explicit -- marker so filenames that begin with a hyphen stay files.
    for ((index = 1; index < COMP_CWORD; index++)); do
        word=${COMP_WORDS[index]}
        if (( options_ended )); then
            has_file_argument=1
            break
        fi
        case "$word" in
            --)
                options_ended=1
                ;;
            -*)
                ;;
            *)
                has_file_argument=1
                break
                ;;
        esac
    done
    if (( has_file_argument )); then
        return 0
    fi

    if (( ! options_ended )) && [[ $cur == -* ]]; then
        COMPREPLY=( $(compgen -W "$options" -- "$cur") )
        return 0
    fi

    # With a blank argument, offer both Dendro's options and the valid local
    # RPM-file/directory choices. This makes the command's capabilities
    # discoverable without hiding its supported positional argument.
    if (( ! options_ended )) && [[ -z $cur ]]; then
        COMPREPLY=( $(compgen -W "$options" -- "$cur") )
    fi

    # Offer directories for navigation and only files Dendro can open.
    while IFS= read -r word; do
        if [[ -d $word || $word == *.rpm ]]; then
            COMPREPLY+=("$word")
        fi
    done < <(compgen -f -- "$cur")
}

complete -F _dendro_completion -o filenames dendro
