# coding: utf-8

# National Intertidal Digital Elevation Model (NIDEM)
# 
# This script generates Geoscience Australia's (GA) National Intertidal Digital Elevation Model (NIDEM) datasets,
# which provide continuous elevation data for Australia's intertidal zone. It initially imports layers from the DEA
# Intertidal Extents Model (ITEM v2.0) and median tidal elevations for each tidal interval, computes elevations at
# interval boundaries, extracts contours around each tidal interval, and then interpolates between these contours
# using TIN/Delaunay triangulation linear interpolation. This interpolation method preserves the tidal interval
# boundaries of ITEM v2.0.
#
# To generate NIDEM datasets:
#
#    1. Set the locations to input datasets in the NIDEM_configuration.ini configuration .ini file
#
#    2. On the NCI, run the NIDEM_pbs_submit.sh shell script which iterates through a set of ITEM polygon tile IDs
#       in parallel. This will call this script (NIDEM_generation.py) which conducts the actual analysis.
#
# NIDEM consists of several output datasets:
#
# 1. The NIDEM dataset (e.g. 'NIDEM_33_130.91_-12.26.tif') provides elevation in metre units relative to modelled
#    Mean Sea Level for each pixel of intertidal terrain across the Australian coastline. The DEMs have been cleaned
#    by masking out non-intertidal pixels and pixels where tidal processes poorly explain patterns of inundation
#    (see NIDEM mask below). This is the primary output product, and is expected to be the default product for most
#    applications. The dataset consists of 306 raster files corresponding to polygons of the ITEM v2.0 continental
#    scale tidal model.
#
# 2. The unfiltered NIDEM dataset (e.g. 'NIDEM_unfiltered_33_130.91_-12.26.tif') provides un-cleaned elevation in
#    metre units relative to modelled Mean Sea Level for each pixel of intertidal terrain across the Australian
#    coastline. Compared to the default NIDEM product, these layers have not been filtered to remove noise,
#    artifacts or invalid elevation values (see NIDEM mask below). This supports applying custom filtering methods
#    to the raw NIDEM data. The dataset consists of 306 raster files corresponding to polygons of the ITEM v2.0
#    continental scale tidal model.
#
# 3. The NIDEM mask dataset (e.g. 'NIDEM_mask_33_130.91_-12.26.tif') flags non-intertidal terrestrial pixels with
#    elevations greater than 25 m (value = 1), and sub-tidal pixels with depths greater than -25 m relative to Mean
#    Sea Level (value = 2). Pixels where tidal processes poorly explain patterns of inundation are also flagged by
#    identifying any pixels with ITEM confidence NDWI standard deviation greater than 0.25 (value = 3). The NIDEM
#    mask was used to filter and clean the NIDEM dataset to remove artifacts and noise (e.g. intertidal pixels in
#    deep water or high elevations) and invalid elevation estimates caused by coastal change or poor model
#    performance. The dataset consists of 306 raster files corresponding to polygons of the ITEM v2.0 continental
#    scale tidal model.
#
# 4. The NIDEM uncertainty dataset (e.g. 'NIDEM_uncertainty_33_130.91_-12.26.tif') provides a measure of the
#    uncertainty (not to be confused with accuracy) of NIDEM elevations in metre units for each pixel. The range
#    of Landsat observation tide heights used to compute median tide heights for each waterline contour can vary
#    significantly between tidal modelling polygons. To quantify this range, the standard deviation of tide heights
#    for all Landsat images used to produce each ITEM interval and subsequent waterline contour was calculated.
#    These values were interpolated to return an estimate of uncertainty for each individual pixel in the NIDEM
#    datasets: larger values indicate the waterline contour was based on a composite of images with a larger range
#    of tide heights. The dataset consists of 306 raster files corresponding to polygons of the ITEM v2.0
#    continental scale tidal model.
#
# 5. The NIDEM waterline contour dataset (e.g. 'NIDEM_contours_33_130.91_-12.26.tif') provides a vector
#    representation of the boundary of every ten percent interval of the observed intertidal range. These contours
#    were extracted along the boundary between each ITEM v2.0 tidal interval, and assigned the median and standard
#    deviation (see NIDEM uncertainty above) of tide heights from the ensemble of corresponding Landsat observations.
#    These datasets facilitate re-analysis by allowing alternative interpolation methods (e.g. kriging, splines) to
#    be used to generate DEMs from median tide heights. The dataset consists of 306 shapefiles corresponding to
#    polygons of the ITEM v2.0 continental scale tidal model.
#
# The filtered, unfiltered, mask & uncertainty products are also exported as 306 combined NetCDF datasets
# corresponding to polygons of the ITEM v2.0 continental scale tidal model (e.g. 'NIDEM_33_130.91_-12.26.nc').
# 
# Date: October 2018
# Author: Robbi Bishop-Taylor, Steven Sagar, Leo Lymburner


#####################################
# Load modules and define functions #
#####################################

import sys
import os
import glob
import fiona
import affine
import numpy as np
import collections
import scipy.interpolate 
from skimage.measure import find_contours
import configparser
from osgeo import gdal
from scipy import ndimage as nd
from shapely.geometry import MultiLineString, mapping
from datacube.model import Variable
from datacube.utils.geometry import Coordinate
from datacube.utils.geometry import CRS
from datacube.storage.storage import create_netcdf_storage_unit
from datacube.storage import netcdf_writer

import pandas as pd
import geopandas as gpd
import datacube
from datacube.utils import geometry
from datacube.api.query import query_group_by
from otps import TimePoint, predict_tide

# Connect to datacube instance
dc = datacube.Datacube(app='NIDEM generation')


##################
# Generate NIDEM #
##################

def main(argv=None):

    if argv is None:

        argv = sys.argv
        print(sys.argv)

    # If no user arguments provided
    if len(argv) < 2:

        str_usage = "You must specify a polygon ID"
        print(str_usage)
        sys.exit()

    # Set ITEM polygon for analysis
    polygon_id = int(argv[1])  # polygon_id = 33

    # Import configuration details from NIDEM_configuration.ini
    config = configparser.ConfigParser()
    config.read('NIDEM_configuration.ini')

    # Set paths to ITEM relative, confidence and offset products
    item_offset_path = config['ITEM inputs']['item_offset_path']
    item_relative_path = config['ITEM inputs']['item_relative_path']
    item_conf_path = config['ITEM inputs']['item_conf_path']
    item_polygon_path = config['ITEM inputs']['item_polygon_path']

    # Set paths to elevation, bathymetry and shapefile datasets used to create NIDEM mask
    srtm30_raster = config['Masking inputs']['srtm30_raster']
    ausbath09_raster = config['Masking inputs']['ausbath09_raster']
    gbr30_raster = config['Masking inputs']['gbr30_raster']
    nthaus30_raster = config['Masking inputs']['nthaus30_raster']

    # Print run details
    print('Processing polygon {} from {}'.format(polygon_id, item_offset_path))

    ##################################
    # Import and prepare ITEM raster #
    ##################################

    # Contours generated by `skimage.measure.find_contours` stop before the edge of nodata pixels. To prevent gaps
    # from occurring between adjacent NIDEM tiles, the following steps 'fill' pixels directly on the boundary of
    # two NIDEM tiles with the value of the nearest pixel with data.

    # Import raster
    item_filename = glob.glob('{}/ITEM_REL_{}_*.tif'.format(item_relative_path, polygon_id))[0]
    item_ds = gdal.Open(item_filename)
    item_array = item_ds.GetRasterBand(1).ReadAsArray()

    # Get coord string of polygon from ITEM array name to use for output names
    coord_str = item_filename[-17:-4]

    # Extract shape, projection info and geotransform data
    yrows, xcols = item_array.shape
    prj = item_ds.GetProjection()
    geotrans = item_ds.GetGeoTransform()
    upleft_x, x_size, x_rotation, upleft_y, y_rotation, y_size = geotrans
    bottomright_x = upleft_x + (x_size * xcols)
    bottomright_y = upleft_y + (y_size * yrows)

    # Identify valid intertidal area by selecting pixels between the lowest and highest ITEM intervals. This is
    # subsequently used to restrict the extent of interpolated elevation data to match the input ITEM polygons.
    valid_intertidal_extent = np.where((item_array > 0) & (item_array < 9), 1, 0)

    # Convert datatype to float to allow assigning nodata -6666 values to NaN
    item_array = item_array.astype('float32')
    item_array[item_array == -6666] = np.nan

    # First, identify areas to be filled by dilating non-NaN pixels by two pixels (i.e. ensuring vertical, horizontal
    # and diagonally adjacent pixels are filled):
    dilated_mask = nd.morphology.binary_dilation(~np.isnan(item_array), iterations=2)

    # For every pixel, identify the indices of the nearest pixel with data (i.e. data pixels will return their own
    # indices; nodata pixels will return the indices of the nearest data pixel). This output can be used to index
    # back into the original array, returning a new array where data pixels remain the same, but every nodata pixel
    # is filled with the value of the nearest data pixel:
    nearest_inds = nd.distance_transform_edt(input=np.isnan(item_array), return_distances=False, return_indices=True)
    item_array = item_array[tuple(nearest_inds)]

    # As we only want to fill pixels on the boundary of NIDEM tiles, set pixels outside the dilated area back to NaN:
    item_array[~dilated_mask] = np.nan

    ##########################################
    # Median and SD tide height per interval #
    ##########################################

    # Each ITEM v2.0 tidal interval boundary was produced from a composite of multiple Landsat images that cover a
    # range of tidal heights. To obtain an elevation relative to modelled mean sea level for each interval boundary,
    # we import a precomputed file containing the median tidal height for all Landsat images that were used to
    # generate the interval (Sagar et al. 2017, https://doi.org/10.1016/j.rse.2017.04.009).

    # Import ITEM offset values for each ITEM tidal interval, dividing by 1000 to give metre units
    item_offsets = np.loadtxt('{}/elevation.txt'.format(item_offset_path), delimiter=',', dtype='str')
    item_offsets = {int(key): [float(val) / 1000.0 for val in value.split(' ')] for (key, value) in item_offsets}
    contour_offsets = item_offsets[polygon_id]

    # The range of tide heights used to compute the above median tide height can vary significantly between tidal
    # modelling polygons. To quantify this range, we take the standard deviation of tide heights for all Landsat
    # images used to produce each ITEM interval. This represents a measure of the 'uncertainty' (not to be confused
    # with accuracy) of NIDEM elevations in m units for each contour. These values are subsequently interpolated to
    # return an estimate of uncertainty for each individual pixel in the NIDEM datasets: larger values indicate the
    # ITEM interval was produced from a composite of images with a larger range of tide heights.

    # Compute uncertainties for each interval, and create a lookup dict to link uncertainties to each NIDEM contour
    uncertainty_array = interval_uncertainty(polygon_id=polygon_id, item_polygon_path=item_polygon_path)
    uncertainty_dict = dict(zip(contour_offsets, uncertainty_array))

    ####################
    # Extract contours #
    ####################

    # Here, we use `skimage.measure.find_contours` to extract contours along the boundary of each ITEM tidal interval
    # (e.g. 0.5 is the boundary between ITEM interval 0 and interval 1; 5.5 is the boundary between interval 5 and
    # interval 6). This function outputs a dictionary with ITEM interval boundaries as keys and lists of xy point
    # arrays as values. Contours are also exported as a shapefile with elevation and uncertainty attributes in metres.
    contour_dict = contour_extract(z_values=np.arange(0.5, 9.5, 1.0),
                                   ds_array=item_array,
                                   ds_crs='EPSG:3577',
                                   ds_affine=geotrans,
                                   output_shp=f'output_data/shapefile/nidem_contours/'
                                              f'NIDEM_contours_{polygon_id}_{coord_str}.shp',
                                   attribute_data={'elev_m': contour_offsets, 'uncert_m': uncertainty_array},
                                   attribute_dtypes={'elev_m': 'float:9.2', 'uncert_m': 'float:9.2'})

    #######################################################################
    # Interpolate contours using TIN/Delaunay triangulation interpolation #
    #######################################################################

    # Here we assign each previously generated contour with its modelled height relative to MSL, producing a set of
    # tidally tagged xyz points that can be used to interpolate elevations across the intertidal zone. We use the
    # linear interpolation method from `scipy.interpolate.griddata`, which computes a TIN/Delaunay triangulation of
    # the input data using Qhull before performing linear barycentric interpolation on each triangle.

    # If contours include valid data, proceed with interpolation
    try:

        # Combine all individual contours for each contour height, and insert a height above MSL column into array
        elev_contours = [np.insert(np.concatenate(v), 2, contour_offsets[i], axis=1) for i, v in
                         enumerate(contour_dict.values())]

        # Combine all contour heights into a single array, and then extract xy points and z-values
        all_contours = np.concatenate(elev_contours)
        points_xy = all_contours[:, [1, 0]]
        values_elev = all_contours[:, 2]

        # Create a matching list of uncertainty values for each xy point
        values_uncert = np.array([np.round(uncertainty_dict[i], 2) for i in values_elev])

        # Calculate bounds of ITEM layer to create interpolation grid (from-to-by values in metre units)
        grid_y, grid_x = np.mgrid[upleft_y:bottomright_y:1j * yrows, upleft_x:bottomright_x:1j * xcols]

        # Interpolate between points onto grid. This uses the 'linear' method from
        # scipy.interpolate.griddata, which computes a TIN/Delaunay triangulation of the input
        # data with Qhull and performs linear barycentric interpolation on each triangle
        print('Interpolating data for polygon {}'.format(polygon_id))
        interp_elev_array = scipy.interpolate.griddata(points_xy, values_elev, (grid_y, grid_x), method='linear')
        interp_uncert_array = scipy.interpolate.griddata(points_xy, values_uncert, (grid_y, grid_x), method='linear')

    except ValueError:

        # If contours contain no valid data, create empty arrays
        interp_elev_array = np.full((yrows, xcols), -9999)
        interp_uncert_array = np.full((yrows, xcols), -9999)

    #########################################################
    # Create ITEM confidence and elevation/bathymetry masks #
    #########################################################

    # The following code applies a range of masks to remove pixels where elevation values are likely to be invalid:
    #
    # 1. Non-coastal terrestrial pixels with elevations greater than 25 m above MSL. This mask is computed using
    #    SRTM-derived 1 Second Digital Elevation Model data (http://pid.geoscience.gov.au/dataset/ga/69769).
    # 2. Sub-tidal pixels with bathymetry values deeper than -25 m below MSL. This mask is computed by identifying
    #    any pixels that are < -25 m in all of the national Australian Bathymetry and Topography Grid
    #    (http://pid.geoscience.gov.au/dataset/ga/67703), gbr30 High-resolution depth model for the Great
    #    Barrier Reef (http://pid.geoscience.gov.au/dataset/ga/115066) and nthaus30 High-resolution depth model
    #    for Northern Australia (http://pid.geoscience.gov.au/dataset/ga/121620).
    # 3. Pixels with high ITEM confidence NDWI standard deviation (i.e. areas where inundation patterns are not driven
    #    by tidal influences). This mask is computed using ITEM v2.0 confidence layer data from DEA.

    # Import ITEM confidence NDWI standard deviation array for polygon
    conf_filename = glob.glob('{}/ITEM_STD_{}_*.tif'.format(item_conf_path, polygon_id))[0]
    conf_ds = gdal.Open(conf_filename)

    # Reproject SRTM-derived 1 Second DEM to cell size and projection of NIDEM
    srtm30_reproj = reproject_to_template(input_raster=srtm30_raster,
                                          template_raster=item_filename,
                                          output_raster='scratch/temp.tif',
                                          nodata_val=-9999)

    # Reproject Australian Bathymetry and Topography Grid to cell size and projection of NIDEM
    ausbath09_reproj = reproject_to_template(input_raster=ausbath09_raster,
                                             template_raster=item_filename,
                                             output_raster='scratch/temp.tif',
                                             nodata_val=-9999)

    # Reproject gbr30 bathymetry to cell size and projection of NIDEM
    gbr30_reproj = reproject_to_template(input_raster=gbr30_raster,
                                         template_raster=item_filename,
                                         output_raster='scratch/temp.tif',
                                         nodata_val=-9999)

    # Reproject nthaus30 bathymetry to cell size and projection of NIDEM
    nthaus30_reproj = reproject_to_template(input_raster=nthaus30_raster,
                                            template_raster=item_filename,
                                            output_raster='scratch/temp.tif',
                                            nodata_val=-9999)

    # Convert raster datasets to arrays
    conf_array = conf_ds.GetRasterBand(1).ReadAsArray()
    srtm30_array = srtm30_reproj.GetRasterBand(1).ReadAsArray()
    ausbath09_array = ausbath09_reproj.GetRasterBand(1).ReadAsArray()
    gbr30_array = gbr30_reproj.GetRasterBand(1).ReadAsArray()
    nthaus30_array = nthaus30_reproj.GetRasterBand(1).ReadAsArray()

    # Convert arrays to boolean masks:
    #  For elevation: any elevations > 25 m in SRTM 30m DEM
    #  For bathymetry: any depths < -25 m in GBR30 AND nthaus30 AND Ausbath09 bathymetry
    #  For ITEM confidence: any cells with NDWI STD > 0.25
    elev_mask = srtm30_array > 25
    bathy_mask = (ausbath09_array < -25) & (gbr30_array < -25) & (nthaus30_array < -25)
    conf_mask = conf_array > 0.25

    # Create a combined mask with -9999 nodata in unmasked areas and where:
    #  1 = elevation mask
    #  2 = bathymetry mask
    #  3 = ITEM confidence mask
    nidem_mask = np.full(item_array.shape, -9999)
    nidem_mask[elev_mask] = 1
    nidem_mask[bathy_mask] = 2
    nidem_mask[conf_mask] = 3

    ################################
    # Export output NIDEM geoTIFFs #
    ################################

    # Because the lowest and highest ITEM intervals (0 and 9) cannot be correctly interpolated as they have no lower
    # or upper bounds, the NIDEM layers are constrained to valid intertidal terrain (ITEM intervals 1-8).
    nidem_uncertainty = np.where(valid_intertidal_extent, interp_uncert_array, -9999).astype(np.float32)
    nidem_unfiltered = np.where(valid_intertidal_extent, interp_elev_array, -9999).astype(np.float32)

    # NIDEM is exported as two DEMs: an unfiltered layer, and a layer that is filtered to remove terrestrial (> 25 m)
    # and sub-tidal terrain (< -25 m) and pixels with high ITEM confidence NDWI standard deviation. Here we mask
    # the unfiltered layer by NIDEM mask to produce a filtered NIDEM layer:
    nidem_filtered = np.where(nidem_mask > 0, -9999, nidem_unfiltered).astype(np.float32)

    # Export filtered NIDEM as a GeoTIFF
    print(f'Exporting filtered NIDEM for polygon {polygon_id}')
    array_to_geotiff(fname=f'output_data/geotiff/nidem/NIDEM_{polygon_id}_{coord_str}.tif',
                     data=nidem_filtered,
                     geo_transform=geotrans,
                     projection=prj,
                     nodata_val=-9999)

    # Export unfiltered NIDEM as a GeoTIFF
    print(f'Exporting unfiltered NIDEM for polygon {polygon_id}')
    array_to_geotiff(fname=f'output_data/geotiff/nidem_unfiltered/NIDEM_unfiltered_{polygon_id}_{coord_str}.tif',
                     data=nidem_unfiltered,
                     geo_transform=geotrans,
                     projection=prj,
                     nodata_val=-9999)

    # Export NIDEM uncertainty layer as a GeoTIFF
    print(f'Exporting NIDEM uncertainty for polygon {polygon_id}')
    array_to_geotiff(fname=f'output_data/geotiff/nidem_uncertainty/NIDEM_uncertainty_{polygon_id}_{coord_str}.tif',
                     data=nidem_uncertainty,
                     geo_transform=geotrans,
                     projection=prj,
                     nodata_val=-9999)

    # Export NIDEM mask as a GeoTIFF
    print(f'Exporting NIDEM mask for polygon {polygon_id}')
    array_to_geotiff(fname=f'output_data/geotiff/nidem_mask/NIDEM_mask_{polygon_id}_{coord_str}.tif',
                     data=nidem_mask.astype(int),
                     geo_transform=geotrans,
                     projection=prj,
                     dtype=gdal.GDT_Int16,
                     nodata_val=-9999)

    ######################
    # Export NetCDF data #
    ######################

    # If netcdf file already exists, delete it
    filename_netcdf = f'output_data/netcdf/NIDEM_{polygon_id}_{coord_str}.nc'

    if os.path.exists(filename_netcdf):
        os.remove(filename_netcdf)

    # Compute coords
    x_coords = netcdf_writer.netcdfy_coord(np.linspace(upleft_x + 12.5, bottomright_x - 12.5, num=xcols))
    y_coords = netcdf_writer.netcdfy_coord(np.linspace(upleft_y - 12.5, bottomright_y + 12.5, num=yrows))

    # Define output compression parameters
    comp_params = dict(zlib=True, complevel=9, shuffle=True, fletcher32=True)

    # Create new dataset
    output_netcdf = create_netcdf_storage_unit(filename=filename_netcdf,
                                               crs=CRS('EPSG:3577'),
                                               coordinates={'x': Coordinate(x_coords, 'metres'),
                                                            'y': Coordinate(y_coords, 'metres')},
                                               variables={'nidem': Variable(dtype=np.dtype('float32'),
                                                                            nodata=-9999,
                                                                            dims=('y', 'x'),
                                                                            units='metres'),
                                                          'nidem_unfiltered': Variable(dtype=np.dtype('float32'),
                                                                                       nodata=-9999,
                                                                                       dims=('y', 'x'),
                                                                                       units='metres'),
                                                          'nidem_uncertainty': Variable(dtype=np.dtype('float32'),
                                                                                        nodata=-9999,
                                                                                        dims=('y', 'x'),
                                                                                        units='metres'),
                                                          'nidem_mask': Variable(dtype=np.dtype('int16'),
                                                                                 nodata=-9999,
                                                                                 dims=('y', 'x'),
                                                                                 units='1')},
                                               variable_params={'nidem': comp_params,
                                                                'nidem_unfiltered': comp_params,
                                                                'nidem_uncertainty': comp_params,
                                                                'nidem_mask': comp_params})

    # dem: assign data and set variable attributes
    output_netcdf['nidem'][:] = netcdf_writer.netcdfy_data(nidem_filtered)
    output_netcdf['nidem'].valid_range = [-25.0, 25.0]
    output_netcdf['nidem'].standard_name = 'height_above_mean_sea_level'
    output_netcdf['nidem'].coverage_content_type = 'modelResult'
    output_netcdf['nidem'].long_name = 'National Intertidal Digital Elevation Model (NIDEM): elevation data in metre ' \
                                       'units relative to mean sea level for each pixel of intertidal terrain across ' \
                                       'the Australian coastline. Cleaned by masking out non-intertidal pixels' \
                                       'and pixels where tidal processes poorly explain patterns of inundation.'

    # dem_unfiltered: assign data and set variable attributes
    output_netcdf['nidem_unfiltered'][:] = netcdf_writer.netcdfy_data(nidem_unfiltered)
    output_netcdf['nidem_unfiltered'].standard_name = 'height_above_mean_sea_level'
    output_netcdf['nidem_unfiltered'].coverage_content_type = 'modelResult'
    output_netcdf['nidem_unfiltered'].long_name = 'NIDEM unfiltered: uncleaned elevation data in metre units ' \
                                                  'relative to mean sea level for each pixel of intertidal terrain ' \
                                                  'across the Australian coastline. Compared to the default NIDEM ' \
                                                  'product, these layers have not been filtered to remove noise, ' \
                                                  'artifacts or invalid elevation values.'

    # uncertainty: assign data and set variable attributes
    output_netcdf['nidem_uncertainty'][:] = netcdf_writer.netcdfy_data(nidem_uncertainty)
    output_netcdf['nidem_uncertainty'].standard_name = 'height_above_mean_sea_level'
    output_netcdf['nidem_uncertainty'].coverage_content_type = 'modelResult'
    output_netcdf['nidem_uncertainty'].long_name = 'NIDEM uncertainty: provides a measure of the uncertainty (not ' \
                                                   'accuracy) of NIDEM elevations in metre units for each pixel. ' \
                                                   'Represents the standard deviation of tide heights of all Landsat ' \
                                                   'observations used to produce each ITEM 2.0 ten percent tidal ' \
                                                   'interval.'

    # mask: assign data and set variable attributes
    output_netcdf['nidem_mask'][:] = netcdf_writer.netcdfy_data(nidem_mask)
    output_netcdf['nidem_mask'].valid_range = [1, 3]
    output_netcdf['nidem_mask'].coverage_content_type = 'qualityInformation'
    output_netcdf['nidem_mask'].long_name = 'NIDEM mask: flags non-intertidal terrestrial pixels with elevations ' \
                                            'greater than 25 m (value = 1), sub-tidal pixels with depths greater ' \
                                            'than -25 m (value = 2), and pixels where tidal processes poorly ' \
                                            'explain patterns of inundation (value = 3).'

    # Add global attributes
    output_netcdf.title = 'National Intertidal Digital Elevation Model 25m 1.0.0'
    output_netcdf.institution = 'Commonwealth of Australia (Geoscience Australia)'
    output_netcdf.product_version = '1.0.0'
    output_netcdf.license = 'CC BY Attribution 4.0 International License'
    output_netcdf.time_coverage_start = '1986-01-01'
    output_netcdf.time_coverage_end = '2016-12-31'
    output_netcdf.cdm_data_type = 'Grid'
    output_netcdf.contact = 'clientservices@ga.gov.au'
    output_netcdf.publisher_email = 'earth.observation@ga.gov.au'
    output_netcdf.source = 'ITEM v2.0'
    output_netcdf.keywords = 'Tidal, Topography, Landsat, Elevation, Intertidal, MSL, ITEM, NIDEM, DEM, Coastal'
    output_netcdf.summary = "The National Intertidal Digital Elevation Model (NIDEM) product is a continental-scale " \
                            "dataset providing continuous elevation data for Australia's exposed intertidal zone. " \
                            "NIDEM provides the first three-dimensional representation of Australia's intertidal " \
                            "zone (excluding off-shore Territories and intertidal mangroves) at 25 m spatial " \
                            "resolution, addressing a key gap between the availability of sub-tidal bathymetry and " \
                            "terrestrial elevation data. NIDEM was generated by combining global tidal modelling " \
                            "with a 30-year time series archive of spatially and spectrally calibrated Landsat " \
                            "satellite data managed within the Digital Earth Australia (DEA) platform. NIDEM " \
                            "complements existing intertidal extent products, and provides data to support a new " \
                            "suite of use cases that require a more detailed understanding of the three-dimensional " \
                            "topography of the intertidal zone, such as hydrodynamic modelling, coastal risk " \
                            "management and ecological habitat mapping."

    # Close dataset
    output_netcdf.close()


def array_to_geotiff(fname, data, geo_transform, projection,
                     nodata_val=0, dtype=gdal.GDT_Float32):

    """
    Create a single band GeoTIFF file with data from an array.

    Because this works with simple arrays rather than xarray datasets from DEA, it requires
    geotransform info ("(upleft_x, x_size, x_rotation, upleft_y, y_rotation, y_size)") and
    projection data (in "WKT" format) for the output raster.

    Last modified: March 2018
    Author: Robbi Bishop-Taylor

    :param fname:
        Output geotiff file path including extension

    :param data:
        Input array to export as a geotiff

    :param geo_transform:
        Geotransform for output raster; e.g. "(upleft_x, x_size, x_rotation,
        upleft_y, y_rotation, y_size)"

    :param projection:
        Projection for output raster (in "WKT" format)

    :param nodata_val:
        Value to convert to nodata in the output raster; default 0

    :param dtype:
        Optionally set the dtype of the output raster; can be useful when exporting
        an array of float or integer values. Defaults to gdal.GDT_Float32

    """

    # Set up driver
    driver = gdal.GetDriverByName('GTiff')

    # Create raster of given size and projection
    rows, cols = data.shape
    dataset = driver.Create(fname, cols, rows, 1, dtype, ['COMPRESS=DEFLATE'])
    dataset.SetGeoTransform(geo_transform)
    dataset.SetProjection(projection)

    # Write data to array and set nodata values
    band = dataset.GetRasterBand(1)
    band.WriteArray(data)
    band.SetNoDataValue(nodata_val)

    # Close file
    dataset = None


def reproject_to_template(input_raster, template_raster, output_raster, resolution=None,
                          resampling=gdal.GRA_Bilinear, nodata_val=0):
    """
    Reprojects a raster to match the extent, cell size, projection and dimensions of a template
    raster using GDAL. Optionally, can set custom resolution for output reprojected raster using
    'resolution'; this will affect raster dimensions/width/columns.

    Last modified: April 2018
    Author: Robbi Bishop-Taylor

    :param input_raster:
        Path to input geotiff raster to be reprojected (.tif)

    :param template_raster:
        Path to template geotiff raster (.tif) used to copy extent, projection etc

    :param output_raster:
        Output reprojected raster path with geotiff extension (.tif)

    :param resolution:
        Optionally set custom cell size for output reprojected raster; defaults to
        'None', or the cell size of template raster

    :param resampling:
        GDAL resampling method to use for reprojection; defaults to gdal.GRA_Bilinear

    :param nodata_val:
        Values in the output reprojected raster to set to nodata; defaults to 0

    :return:
        GDAL dataset for further analysis, and raster written to output_raster (if this
        dataset appears empty when loaded into a GIS, close the dataset like 'output_ds = None')

    """

    # Import raster to reproject
    print("Importing raster datasets")
    input_ds = gdal.Open(input_raster)
    input_proj = input_ds.GetProjection()
    input_geotrans = input_ds.GetGeoTransform()
    data_type = input_ds.GetRasterBand(1).DataType
    n_bands = input_ds.RasterCount

    # Import raster to use as template
    template_ds = gdal.Open(template_raster)
    template_proj = template_ds.GetProjection()
    template_geotrans = template_ds.GetGeoTransform()
    template_w = template_ds.RasterXSize
    template_h = template_ds.RasterYSize

    # Use custom resolution if supplied
    if resolution:
        template_geotrans[1] = float(resolution)
        template_geotrans[-1] = -float(resolution)

    # Create new output dataset to reproject into
    output_ds = gdal.GetDriverByName('Gtiff').Create(output_raster, template_w,
                                                     template_h, n_bands, data_type)
    output_ds.SetGeoTransform(template_geotrans)
    output_ds.SetProjection(template_proj)
    output_ds.GetRasterBand(1).SetNoDataValue(nodata_val)

    # Reproject raster into output dataset
    print("Reprojecting raster")
    gdal.ReprojectImage(input_ds, output_ds, input_proj, template_proj, resampling)

    # Close datasets
    input_ds = None
    template_ds = None

    print("Reprojected raster exported to {}".format(output_raster))
    return output_ds


def contour_extract(z_values, ds_array, ds_crs, ds_affine, output_shp=None, min_vertices=2,
                    attribute_data=None, attribute_dtypes=None):

    """
    Uses `skimage.measure.find_contours` to extract contour lines from a two-dimensional array.
    Contours are extracted as a dictionary of xy point arrays for each contour z-value, and optionally as
    line shapefile with one feature per contour z-value.

    The `attribute_data` and `attribute_dtypes` parameters can be used to pass custom attributes to the output
    shapefile.

    Last modified: September 2018
    Author: Robbi Bishop-Taylor

    :param z_values:
        A list of numeric contour values to extract from the array.

    :param ds_array:
        A two-dimensional array from which contours are extracted. This can be a numpy array or xarray DataArray.
        If an xarray DataArray is used, ensure that the array has one two dimensions (e.g. remove the time dimension
        using either `.isel(time=0)` or `.squeeze('time')`).

    :param ds_crs:
        Either a EPSG string giving the coordinate system of the array (e.g. 'EPSG:3577'), or a crs
        object (e.g. from an xarray dataset: `xarray_ds.geobox.crs`).

    :param ds_affine:
        Either an affine object from a rasterio or xarray object (e.g. `xarray_ds.geobox.affine`), or a gdal-derived
        geotransform object (e.g. `gdal_ds.GetGeoTransform()`) which will be converted to an affine.

    :param min_vertices:
        An optional integer giving the minimum number of vertices required for a contour to be extracted. The default
        (and minimum) value is 2, which is the smallest number required to produce a contour line (i.e. a start and
        end point). Higher values remove smaller contours, potentially removing noise from the output dataset.

    :param output_shp:
        An optional string giving a path and filename for the output shapefile. Defaults to None, which
        does not generate a shapefile.

    :param attribute_data:
        An optional dictionary of lists used to define attributes/fields to add to the shapefile. Dict keys give
        the name of the shapefile attribute field, while dict values must be lists of the same length as `z_values`.
        For example, if `z_values=[0, 10, 20]`, then `attribute_data={'type: [1, 2, 3]}` can be used to create a
        shapefile field called 'type' with a value for each contour in the shapefile. The default is None, which
        produces a default shapefile field called 'z_value' with values taken directly from the `z_values` parameter
        and formatted as a 'float:9.2'.

    :param attribute_dtypes:
        An optional dictionary giving the output dtype for each shapefile attribute field that is specified by
        `attribute_data`. For example, `attribute_dtypes={'type: 'int'}` can be used to set the 'type' field to an
        integer dtype. The dictionary should have the same keys/field names as declared in `attribute_data`.
        Valid values include 'int', 'str', 'datetime, and 'float:X.Y', where X is the minimum number of characters
        before the decimal place, and Y is the number of characters after the decimal place.

    :return:
        A dictionary with contour z-values as the dict key, and a list of xy point arrays as dict values.

    """

    # First test that input array has only two dimensions:
    if len(ds_array.shape) == 2:

        # Obtain affine object from either rasterio/xarray affine or a gdal geotransform:
        if type(ds_affine) != affine.Affine:

            ds_affine = affine.Affine.from_gdal(*ds_affine)

        ####################
        # Extract contours #
        ####################

        # Output dict to hold contours for each offset
        contours_dict = collections.OrderedDict()

        for z_value in z_values:

            # Extract contours and convert output array pixel coordinates into arrays of real world Albers coordinates.
            # We need to add (0.5 x the pixel size) to x values and subtract (-0.5 * pixel size) from y values to
            # correct coordinates to give the centre point of pixels, rather than the top-left corner
            print(f'Extracting contour {z_value}')
            ps = ds_affine[0]  # Compute pixel size
            contours_geo = [np.column_stack(ds_affine * (i[:, 1], i[:, 0])) + np.array([0.5 * ps, -0.5 * ps]) for i in
                            find_contours(ds_array, z_value)]

            # For each array of coordinates, drop any xy points that have NA
            contours_nona = [i[~np.isnan(i).any(axis=1)] for i in contours_geo]

            # Drop 0 length and add list of contour arrays to dict
            contours_withdata = [i for i in contours_nona if len(i) >= min_vertices]

            # If there is data for the contour, add to dict:
            if len(contours_withdata) > 0:
                contours_dict[z_value] = contours_withdata
            else:
                print(f'    No data for contour {z_value}; skipping')

        #######################
        # Export to shapefile #
        #######################

        # If a shapefile path is given, generate shapefile
        if output_shp:

            print(f'\nExporting contour shapefile to {output_shp}')

            # If attribute fields are left empty, default to including a single z-value field based on `z_values`
            if not attribute_data:

                # Default field uses two decimal points by default
                attribute_data = {'z_value': z_values}
                attribute_dtypes = {'z_value': 'float:9.2'}

            # Set up output multiline shapefile properties
            schema = {'geometry': 'MultiLineString',
                      'properties': attribute_dtypes}

            # Create output shapefile for writing
            with fiona.open(output_shp, 'w',
                            crs={'init': str(ds_crs), 'no_defs': True},
                            driver='ESRI Shapefile',
                            schema=schema) as output:

                # Write each shapefile to the dataset one by one
                for i, (z_value, contours) in enumerate(contours_dict.items()):

                    # Create multi-string object from all contour coordinates
                    contour_multilinestring = MultiLineString(contours)

                    # Get attribute values for writing
                    attribute_vals = {field_name: field_vals[i] for field_name, field_vals in attribute_data.items()}

                    # Write output shapefile to file with z-value field
                    output.write({'properties': attribute_vals,
                                  'geometry': mapping(contour_multilinestring)})

        # Return dict of contour arrays
        return contours_dict

    else:
        print(f'The input `ds_array` has shape {ds_array.shape}. Please input a two-dimensional array (if your '
              f'input array has a time dimension, remove it using `.isel(time=0)` or `.squeeze(\'time\')`)')


def interval_uncertainty(polygon_id, item_polygon_path,
                         products=('ls5_pq_albers', 'ls7_pq_albers', 'ls8_pq_albers'),
                         time_period=('1986-01-01', '2017-01-01')):

    """
    This function uses the Digital Earth Australia archive to compute the standard deviation of tide heights for all
    Landsat observations that were used to generate the ITEM 2.0 composite layers and resulting tidal intervals. These
    standard deviations (one for each ITEM 2.0 interval) quantify the 'uncertainty' of each NIDEM elevation estimate:
    larger values indicate the ITEM interval was produced from a composite of images with a larger range of tide
    heights.

    Last modified: September 2018
    Author: Robbi Bishop-Taylor

    :param polygon_id:
        An integer giving the polygon ID of the desired ITEM v2.0 polygon to analyse.

    :param item_polygon_path:
        A string giving the path to the ITEM v2.0 polygon shapefile.

    :param products:
        An optional tuple of DEA Landsat product names used to calculate tide heights of all observations used
        to generate ITEM v2.0 tidal intervals. Defaults to ('ls5_pq_albers', 'ls7_pq_albers', 'ls8_pq_albers'),
        which loads Landsat 5, Landsat 7 and Landsat 8.

    :param time_period:
        An optional tuple giving the start and end date to analyse. Defaults to ('1986-01-01', '2017-01-01'), which
        analyses all Landsat observations from the start of 1986 to the end of 2016.

    :return:
        An array of shape (9,) giving the standard deviation of tidal heights for all Landsat observations used to
        produce each ITEM interval.

    """

    # Import tidal model data and extract geom and tide post
    item_gpd = gpd.read_file(item_polygon_path)
    lat, lon, poly = item_gpd[item_gpd.ID == int(polygon_id)][['lat', 'lon', 'geometry']].values[0]
    geom = geometry.Geometry(mapping(poly), crs=geometry.CRS(item_gpd.crs['init']))

    all_times_obs = list()

    # For each product:
    for source in products:

        # Use entire time range unless LS7
        time_range = ('1986-01-01', '2003-05-01') if source == 'ls7_pq_albers' else time_period

        # Determine matching datasets for geom area and group into solar day
        ds = dc.find_datasets(product=source, time=time_range, geopolygon=geom)
        group_by = query_group_by(group_by='solar_day')
        sources = dc.group_datasets(ds, group_by)

        # If data is found, add time to list then sort
        if len(ds) > 0:
            all_times_obs.extend(sources.time.data.astype('M8[s]').astype('O').tolist())

    # Calculate tide data from X-Y-time location
    all_times_obs = sorted(all_times_obs)
    tp_obs = [TimePoint(float(lon), float(lat), dt) for dt in all_times_obs]
    tides_obs = [tide.tide_m for tide in predict_tide(tp_obs)]

    # Covert to dataframe of observed dates and tidal heights
    df1_obs = pd.DataFrame({'Tide_height': tides_obs}, index=pd.DatetimeIndex(all_times_obs))


    ##################
    # ITEM intervals #
    ##################

    # Compute percentage tide height
    min_height = df1_obs.Tide_height.min()
    max_height = df1_obs.Tide_height.max()
    observed_range = max_height - min_height

    # Create dict of percentile values
    per10_dict = {perc + 1: min_height + observed_range * perc * 0.1 for perc in range(0, 10, 1)}

    # Bin each observation into an interval
    df1_obs['interval'] = pd.cut(df1_obs.Tide_height,
                                 bins=list(per10_dict.values()),
                                 labels=list(per10_dict.keys())[:-1])

    return df1_obs.groupby('interval').std().values.flatten()


if __name__ == "__main__":
    main()
