import os
import sys
import json
import pandas as pd
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scenario_config import scenario_from_cli


def collect_swap_records(input_path):
    swap_records = []

    # Verify directory exists and gather relevant files
    if os.path.exists(input_path):
        files_to_process = [
            f for f in os.listdir(input_path)
            if f.endswith(".json") or f.endswith(".geojson")
        ]

        # Process files with a progress bar
        for file_name in tqdm(files_to_process, desc="Processing GeoJSON files", unit="file"):
            file_filepath = os.path.join(input_path, file_name)

            with open(file_filepath, "r") as f:
                data = json.load(f)

            # Filter and parse GeoJSON features
            features = data.get("features", [])
            for feature in features:
                geometry_type = feature.get("geometry", {}).get("type")
                properties = feature.get("properties", {})

                # The stop at the station, not the drive to it. Both can be
                # Points with type "to_swap": when an agent is already standing
                # on the station node the drive collapses to zero length, and
                # the trip feature is written out as a Point like the wait that
                # follows it. Matching on geometry type alone counted that
                # agent's battery twice -- the pair shows up as a ten-second
                # record with no facility_id immediately before the real one,
                # carrying the same total_distance_m.
                #
                # segment_times is the discriminator rather than facility_id:
                # every trip feature carries it (empty when routing failed) and
                # no stop feature does, so this holds even for a run written
                # before stops recorded which station they were at.
                is_stop = "segment_times" not in properties
                if (geometry_type == "Point" and is_stop
                        and properties.get("type") == "to_swap"):
                    record = {
                        "agent_id": properties.get("agent"),
                        "swap_station": properties.get("facility_id"),
                        "arrival_distance": properties.get("total_distance_m"),
                        "arrival_time": properties.get("start_time"),
                        "departure_time": properties.get("end_time")
                    }
                    swap_records.append(record)

    return swap_records


def main():
    scenario = scenario_from_cli("Export swap station visits to Excel")
    input_path = scenario.trips_time_dir
    output_path = scenario.output_dir

    # The tracks are the whole input. collect_swap_records shrugs at a missing
    # folder and returns nothing, which used to become a spreadsheet of zero
    # rows -- indistinguishable, once on disk, from a run in which no vehicle
    # ever swapped, and read as exactly that by the charging analysis
    # downstream. An empty tracks folder is the case that actually happens: an
    # interrupted run leaves the folder behind with nothing in it, and the
    # board offers this task because the folder exists.
    #
    # Zero swap records from tracks that ARE there is left alone: that is a
    # real result about a real run, not an absent input.
    if not os.path.isdir(input_path) or not [
            f for f in os.listdir(input_path)
            if f.endswith(".json") or f.endswith(".geojson")]:
        raise SystemExit(
            f"No agent tracks in {input_path}.\n"
            f"Run the simulation first -- swap visits are read out of the "
            f"tracks it writes.")

    swap_records = collect_swap_records(input_path)

    # Create DataFrame and export to Excel
    print("\nExporting to Excel...")
    df_swaps = pd.DataFrame(swap_records)

    os.makedirs(output_path, exist_ok=True)
    excel_output_path = os.path.join(output_path, "swap_station_activity.xlsx")
    df_swaps.to_excel(excel_output_path, index=False)

    print(f"Done! Exported {len(df_swaps)} records to {excel_output_path}")


if __name__ == "__main__":
    main()