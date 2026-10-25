# Shared helpers for the scripts in this folder (sourced, not run).

# set_secret <file in secrets/> <KEY> <value>
# Sets KEY=value in secrets/<file>, creating it from secrets/<file>.example
# if needed. The file stays readable by its owner only.
set_secret() {
    file="secrets/$1"
    mkdir -p secrets
    chmod 700 secrets
    if [ ! -f "$file" ]; then
        if [ -f "$file.example" ]; then cp "$file.example" "$file"; else : > "$file"; fi
    fi
    chmod 600 "$file"
    tmp=$(mktemp "secrets/.tmp.XXXXXX")
    awk -v key="$2" -v value="$3" '
        BEGIN { done = 0 }
        $0 ~ "^" key "=" { print key "=" value; done = 1; next }
        { print }
        END { if (!done) print key "=" value }
    ' "$file" > "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$file"
}
