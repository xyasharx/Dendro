# SPDX-License-Identifier: GPL-3.0-or-later
# Bash completion for the dendro command.

_dendro_completion()
{
    local cur word
    local -i has_file_argument=0 index

    cur=${COMP_WORDS[COMP_CWORD]}
    COMPREPLY=()

    if [[ $cur == -* ]]; then
        COMPREPLY=( $(compgen -W '-h --help -v --version -d --debug' -- "$cur") )
        return 0
    fi

    # Dendro accepts at most one positional argument: a local RPM file.
    # Do not offer another file once one has already been supplied.
    for ((index = 1; index < COMP_CWORD; index++)); do
        word=${COMP_WORDS[index]}
        case "$word" in
            --|-*)
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

    # Offer directories for navigation and only files Dendro can open.
    while IFS= read -r word; do
        if [[ -d $word || $word == *.rpm ]]; then
            COMPREPLY+=("$word")
        fi
    done < <(compgen -f -- "$cur")
}

complete -F _dendro_completion -o filenames dendro
