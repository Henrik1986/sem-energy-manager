
#!/bin/sh

LAT="$1"
LON="$2"
DEC="$3"
AZ="$4"
KWP="$5"
DAMPING_MORNING="$6"
DAMPING_EVENING="$7"

URL="https://api.forecast.solar/estimate/${LAT}/${LON}/${DEC}/${AZ}/${KWP}"

curl -fsS -G "$URL" \
  --data-urlencode 'time=iso8601' \
  --data-urlencode "damping=${DAMPING_MORNING},${DAMPING_EVENING}" \
  -H 'accept: text/csv' \
  -H 'X-Delimiter: |' \
  -H 'X-Separator: ;' |
jq -Rn '
  {
    sem: [
      inputs
      | split(";")
      | {
          label: .[0],
          date: .[1],
          values: (.[2] | gsub("\r"; "") | tonumber)
        }
    ]
    | group_by(.label)
    | map({key: .[0].label, value: .})
    | from_entries
  }
'