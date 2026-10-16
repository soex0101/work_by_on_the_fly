import math
import requests
import os
from io import BytesIO
from pyproj import Transformer
from PIL import Image
#from reportlab.pdfgen import canvas
from osgeo import gdal, osr
import numpy as np

from read_settings import load_config
cfg = load_config()

def transform_and_clip_image_pillow(
    pil_image, world_file_vars, src_srs, dst_srs, clip_bbox
):
    """
    Transform and clip a PIL Image using GDAL without writing files.

    Args:
        pil_image (Image): Pillow Image object.
        world_file_vars (tuple): World file variables (x_min, y_max, pixel_size_x, pixel_size_y).
        src_srs (str): Source spatial reference (e.g., 'EPSG:31468').
        dst_srs (str): Target spatial reference (e.g., 'EPSG:25832').
        clip_bbox (tuple): Clipping bounding box (xmin, ymin, xmax, ymax) in target CRS.

    Returns:
        Image: Transformed and clipped Pillow Image.
    """
    # Extract world file variables
    x_min, y_max, pixel_size_x, pixel_size_y = world_file_vars

    # Convert Pillow Image to numpy array
    array = np.array(pil_image)

    # Create an in-memory GDAL dataset from the numpy array
    rows, cols = array.shape[:2]
    driver = gdal.GetDriverByName("MEM")
    mem_ds = driver.Create("", cols, rows, array.shape[2] if len(array.shape) > 2 else 1, gdal.GDT_Byte)

    # Set the geotransform and projection
    mem_ds.SetGeoTransform((x_min, pixel_size_x, 0, y_max, 0, -pixel_size_y))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(int(src_srs.split(":")[1]))  # Extract EPSG code
    mem_ds.SetProjection(srs.ExportToWkt())

    # Add the image data to the dataset
    if len(array.shape) == 3:  # RGB or RGBA
        for i in range(array.shape[2]):
            mem_ds.GetRasterBand(i + 1).WriteArray(array[:, :, i])
    else:  # Grayscale
        mem_ds.GetRasterBand(1).WriteArray(array)

    # Transform the dataset
    transformed_ds = gdal.Warp(
        "",  # Output to in-memory dataset
        mem_ds,
        dstSRS=dst_srs,
        format="MEM",
        resampleAlg="lanczos", # cubic
    )

    # Clip the transformed dataset
    clipped_ds = gdal.Translate(
        "",  # Output to in-memory dataset
        transformed_ds,
        projWin=(clip_bbox[0], clip_bbox[3], clip_bbox[2], clip_bbox[1]),  # xmin, ymax, xmax, ymin
        format="MEM",
    )

    # Convert the clipped dataset back to a Pillow Image
    clipped_array = clipped_ds.ReadAsArray()
    if len(clipped_array.shape) == 3:  # RGB or RGBA
        clipped_image = Image.fromarray(np.dstack(clipped_array))
    else:  # Grayscale
        clipped_image = Image.fromarray(clipped_array)

    return clipped_image


# Constants for scales
scale_data = {
    500: {
        "baseZoom": 15,
        "resolution": 0.125,
        "tile": {"lat": 32, "lon": 32},
    },
    1000: {
        "baseZoom": 14,
        "resolution": 0.25,
        "tile": {"lat": 64, "lon": 64},
    },
    2000: {
        "baseZoom": 13,
        "resolution": 0.5,
        "tile": {"lat": 128, "lon": 128},
    },
}

# Constants for map extent
max_extent = {
    "left": 3925712,
    "right": 4974288,
    "top": 5924288,
    "bottom": 4875712,
}

base_url = "https://c1.bayern.de/betty4/alkis"

def get_transformer(epsg):
    """Get a transformer for the specified EPSG code."""
    return Transformer.from_crs(f"EPSG:{epsg}", "EPSG:31468", always_xy=True), Transformer.from_crs("EPSG:31468", f"EPSG:{epsg}", always_xy=True)

def get_transformed_bbox(transformer, bbox):
    ax, ay = transformer[0].transform(bbox[0], bbox[1])
    bx, by = transformer[0].transform(bbox[2], bbox[1])
    cx, cy = transformer[0].transform(bbox[0], bbox[3])
    dx, dy = transformer[0].transform(bbox[2], bbox[3])
    min_x = min(ax, bx, cx, dx)
    max_x = max(ax, bx, cx, dx)
    min_y = min(ay, by, cy, dy)
    max_y = max(ay, by, cy, dy)
    return min_x, min_y, max_x, max_y

# def write_world_file(worldFile, bbox, width, height):
#     f = open(worldFile, 'w')
#     (min_x, min_y, max_x, max_y) = bbox
#     xscale = (max_x - min_x) / width
#     yscale = (min_y - max_y) / height
#     f.write(str(xscale) + "\n")
#     f.write("0\n0\n")
#     f.write(str(yscale) + "\n")
#     f.write(str(min_x) + "\n")
#     f.write(str(max_y))
#     f.close()

def get_xyz(bbox, scale):
    """Calculate the XYZ tile coordinates for a given BBOX and scale, with additional rows in each direction."""
    base_zoom = scale_data[scale]["baseZoom"]

    # Transform BBOX from input EPSG to Gauss-Krueger 4
    (min_x, min_y, max_x, max_y) = bbox

    # Convert geographical BBOX to tile indices
    x_min = math.floor((min_x - max_extent["left"]) / math.pow(2, 20 - base_zoom)) - 1
    y_min = math.floor((max_extent["top"] - max_y) / math.pow(2, 20 - base_zoom)) - 1
    x_max = math.floor((max_x - max_extent["left"]) / math.pow(2, 20 - base_zoom)) + 1
    y_max = math.floor((max_extent["top"] - min_y) / math.pow(2, 20 - base_zoom)) + 1

    return [(x, y, base_zoom) for x in range(x_min, x_max + 1) for y in range(y_min, y_max + 1)]

def xyz_to_url(x, y, z):
    """Construct the URL for the tile."""
    return f"{base_url}/{z}/{x}/{y}"

def download_tiles_to_buffer(bbox, scale):
    """Download tiles for a given BBOX and scale into a buffer."""
    xyz_tiles = get_xyz(bbox, scale)
    buffers = {}
    output_dir = r"./tiles"

    for x, y, z in xyz_tiles:
        filename = os.path.join(output_dir, f"tile_{x}_{y}_{z}.png")  # or .jpg depending on tile format
        # check file exist
        if os.path.exists(filename):
            with open(filename, 'rb') as f:
                buffer = BytesIO(f.read())
                buffers[(x, y, z)] = buffer
            continue    

        url = xyz_to_url(x, y, z)
        print(f"Downloading: {url}")
        response = requests.get(url)
        if response.status_code == 200:
            buffers[(x, y, z)] = BytesIO(response.content)

            # Save the buffer content to a file
            with open(filename, 'wb') as f:
                f.write(response.content)  # Save tile content to file
            print(f"Saved tile: {filename}")
        else:
            print(f"Failed to download tile: {url}")

    return buffers, xyz_tiles

def merge_tiles_from_buffer(buffers, xyz_tiles):
    """Merge tiles from buffer into a single image."""
    x_values = sorted(set(x for x, y, z in xyz_tiles))
    y_values = sorted(set(y for x, y, z in xyz_tiles))

    tile_width, tile_height = 256, 256  # Assuming tiles are 256x256 pixels
    merged_image = Image.new("RGB", (tile_width * len(x_values), tile_height * len(y_values)))

    for (x, y, z), buffer in buffers.items():
        tile_image = Image.open(buffer)
        x_offset = (x - x_values[0]) * tile_width
        y_offset = (y - y_values[0]) * tile_height
        merged_image.paste(tile_image, (x_offset, y_offset))

    return merged_image, x_values, y_values, tile_width, tile_height

def crop_to_bbox(merged_image, bbox, x_values, y_values, tile_width, tile_height, scale):
    """Crop the merged image to the input BBOX dimensions."""
    # Transform BBOX to Gauss-Krueger 4
    (min_x, min_y, max_x, max_y) = bbox
    
    # Calculate pixel coordinates for BBOX
    x_min_px = ((min_x - max_extent["left"]) / math.pow(2, 20 - scale_data[scale]["baseZoom"]) - x_values[0]) * tile_width
    y_min_px = ((max_extent["top"] - max_y) / math.pow(2, 20 - scale_data[scale]["baseZoom"]) - y_values[0]) * tile_height
    x_max_px = ((max_x - max_extent["left"]) / math.pow(2, 20 - scale_data[scale]["baseZoom"]) - x_values[0]) * tile_width
    y_max_px = ((max_extent["top"] - min_y) / math.pow(2, 20 - scale_data[scale]["baseZoom"]) - y_values[0]) * tile_height

    # Crop the merged image to the specified BBOX dime
    cropped_image = merged_image.crop((x_min_px, y_min_px, x_max_px, y_max_px))
    return cropped_image

if __name__ == "__main__":
    # Input parameters
    scale = cfg.scale
    epsg = 25832  # Example EPSG code
    bbox = list(cfg.bbox)
    gap = 30
    bbox_extended = [bbox[0] - gap, bbox[1] - gap, bbox[2] + gap, bbox[3] + gap]
    transformer = get_transformer(epsg)
    bbox_transformed = get_transformed_bbox(transformer, bbox_extended)

    print(f"{bbox_transformed}")

    buffers, xyz_tiles = download_tiles_to_buffer(bbox_transformed, scale)
    merged_image, x_values, y_values, tile_width, tile_height = merge_tiles_from_buffer(buffers, xyz_tiles)
    cropped_image = crop_to_bbox(merged_image, bbox_transformed, x_values, y_values, tile_width, tile_height, scale)

    #cropped_image = cropped_image.resize((533, 652), Image.Resampling.LANCZOS)
    #cropped_image.show()  # Display the cropped image
    #cropped_image.save("cropped_image.png", quality=97, dpi=(72, 72))  # Save the cropped image

    width, height = cropped_image.size
    #write_world_file("cropped_image.pgw", bbox_transformed, width, height)
    #generate_pdf_from_image(cropped_image, "cropped_image.pdf")

    resultImg = transform_and_clip_image_pillow(cropped_image,
                                                (bbox_transformed[0],
                                                     bbox_transformed[3],
                                                     (bbox_transformed[2] - bbox_transformed[0]) / width,
                                                     (bbox_transformed[3] - bbox_transformed[1]) / height),
                                                "EPSG:31468",
                                                "EPSG:25832",
                                                bbox)
    resultImg.save("cropped_image.png", dpi=(72, 72))  # Save the cropped image

