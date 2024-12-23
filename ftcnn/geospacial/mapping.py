import os
from os import PathLike
from pathlib import Path
from typing import Callable, Union

import geopandas as gpd
import rasterio
from PIL import Image
from shapely.geometry import Polygon
from tqdm.auto import tqdm

from ftcnn.geometry.polygons import create_tile_polygon
from ftcnn.geospacial.utils import parse_filename
from ftcnn.io import collect_files_with_suffix
from ftcnn.raster import create_window, open_raster


def map_metadata(
    gdf_src: gpd.GeoDataFrame,
    images_dir: PathLike,
    parse_filename: Callable = parse_filename,
    preserve_fields: (
        Union[list[Union[str, dict[str, str]]], dict[str, str]] | None
    ) = None,
) -> gpd.GeoDataFrame:
    """
    Maps metadata for images referenced in a GeoDataFrame, with flexible field preservation and renaming.
    Ensures that specified columns exist in the source DataFrame before preservation or renaming.

    Parameters:
        gdf_src (gpd.GeoDataFrame): Source GeoDataFrame containing metadata.
        img_dir (PathLike): Directory containing image files.
        parse_filename (Callable): Function to derive filenames from GeoDataFrame rows.
        preserve_fields (Union[List[Union[str, Dict[str, str]]], Dict[str, str]], optional):
            Specifies fields to preserve from the original GeoDataFrame.
            Can be:
                - A list of strings: Columns to preserve as-is.
                - A list of dictionaries: Specifies renaming with `{new_name: old_name}`.
                - A dictionary: Specifies renaming in `{new_name: old_name}` format.

    Returns:
        gpd.GeoDataFrame: A GeoDataFrame with image metadata and preserved/renamed fields.

    Raises:
        KeyError: If any column to preserve does not exist in the original DataFrame.
    """
    images_dir = Path(images_dir).resolve()
    columns = ["filename", "path", "width", "height", "bbox"]
    rows = []
    geometry = []

    # Normalize preserve_fields to a single dictionary
    field_map = {}
    if preserve_fields:
        if isinstance(preserve_fields, dict):
            field_map = preserve_fields
        elif isinstance(preserve_fields, list):
            for item in preserve_fields:
                if isinstance(item, str):
                    field_map[item] = item  # Preserve as-is
                elif isinstance(item, dict):
                    field_map.update(item)  # Add renaming mappings
    if len(field_map):
        columns.extend(field_map.keys())

    for _, row in gdf_src.iterrows():
        filename = parse_filename(row)
        path = images_dir / filename

        if path.exists():
            # Skip duplicate paths
            if any(r["path"] == path for r in rows):
                continue

            suffix = path.suffix
            open_fn = open_raster if suffix in [".tiff", ".tif"] else Image.open

            with open_fn(path) as img:
                # Handle width and height based on image type
                width, height = (
                    img.size
                    if isinstance(img, Image.Image)
                    else (img.shape[1], img.shape[0])
                )

                # Base metadata
                metadata = {
                    "filename": filename,
                    "path": str(path),
                    "width": width,
                    "height": height,
                    "bbox": row.get("bbox", None),
                }

                # Add preserved fields with existence check
                for new_col, old_col in field_map.items():
                    if old_col not in row:
                        raise KeyError(
                            f"Column '{old_col}' does not exist in the source DataFrame."
                        )
                    metadata[new_col] = row.get(old_col)

                rows.append(metadata)
                geometry.append(row.get("geometry", None))

    return gpd.GeoDataFrame(
        rows,
        columns=columns,
        geometry=geometry,
        crs=gdf_src.crs,
    )


def map_geometry_to_geotiffs(
    gdf: gpd.GeoDataFrame, img_dir: PathLike, recurse: bool = True
) -> gpd.GeoDataFrame:
    """
    Maps geometries in a GeoDataFrame to corresponding GeoTIFF files based on spatial intersections.

    Parameters:
        gdf (gpd.GeoDataFrame): The GeoDataFrame containing geometries to map.
        img_dir (PathLike): The directory containing GeoTIFF files to map geometries to.
        recurse (bool): Whether to search for GeoTIFFs recursively within the directory. Defaults to True.

    Returns:
        gpd.GeoDataFrame: A new GeoDataFrame containing metadata for each GeoTIFF, including the intersecting geometries.
    """
    img_dir = Path(img_dir).resolve()

    columns = [
        "filename",
        "path",
        "width",
        "height",
    ]
    rows = []
    geometry = []

    orig_stems = [
        os.path.splitext(filename)[0] for filename in gdf["filename"].unique().tolist()
    ]

    # Helper function to compare a stem against a list of names.
    def compare_stem(stem, names):
        for name in names:
            if stem[: len(name)] in name:
                return True
        return False

    image_paths = [
        path
        for path in collect_files_with_suffix(".tif", img_dir, recurse=recurse)
        if compare_stem(path.stem, orig_stems)
    ]

    for path in tqdm(
        image_paths,
        desc="Mapping geometry to GeoTIFFs",
        leave=False,
    ):
        with rasterio.open(path) as src:
            # Create a window that represents the full extent of the GeoTIFF.
            tile_window = create_window(0, 0, src.width, src.height)

            # Create a polygon representing the bounds of the GeoTIFF.
            tile_polygon = create_tile_polygon(src, tile_window)

            # Find polygons in the GeoDataFrame that intersect with the GeoTIFF polygon.
            intersecting_polygons = gdf.loc[gdf.intersects(tile_polygon)]

            row = {
                "filename": path.name,
                "path": str(path),
                "width": src.width,
                "height": src.height,
            }

            # If intersecting polygons are found, add them to the output lists.
            if not intersecting_polygons.empty:
                for _, polygon_row in intersecting_polygons.iterrows():
                    geometry.append(polygon_row["geometry"].intersection(tile_polygon))
                    rows.append(row)
            else:
                # If no intersections, append an empty polygon for completeness.
                geometry.append(Polygon())
                rows.append(row)

    return gpd.GeoDataFrame(
        gpd.GeoDataFrame(rows, columns=columns, geometry=geometry, crs=gdf.crs)
        .explode()
        .drop_duplicates()
    )
