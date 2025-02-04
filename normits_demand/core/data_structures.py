# -*- coding: utf-8 -*-
"""
Created on: Tues May 25th 15:04:32 2021
Updated on:

Original author: Ben Taylor
Last update made by:
Other updates made by:

File purpose:
Holds custom normits_demand objects, such as DVector and its functions
"""
# Allow class self type hinting
from __future__ import annotations

# Builtins
import os
import math
import enum
import copy
import pickle
import pathlib
import warnings
import operator
import itertools

from typing import Any
from typing import Dict
from typing import List
from typing import Union
from typing import Callable

# Third Party
import numpy as np
import pandas as pd

# Local Imports
import normits_demand as nd
from normits_demand import constants as consts
from normits_demand import core

from normits_demand.utils import general as du
from normits_demand.utils import pandas_utils as pd_utils

from normits_demand.utils import file_ops
from normits_demand.utils import compress

from normits_demand.concurrency import multiprocessing


# ## CLASSES ## #
# Define valid time formats
@enum.unique
class TimeFormat(enum.Enum):
    AVG_WEEK = 'avg_week'
    AVG_DAY = 'avg_day'
    AVG_HOUR = 'avg_hour'

    @classmethod
    def get_time_periods(cls) -> List[int]:
        return [1, 2, 3, 4, 5, 6]

    @classmethod
    def conversion_order(cls) -> List[TimeFormat]:
        return [cls.AVG_WEEK, cls.AVG_DAY, cls.AVG_HOUR]

    @classmethod
    def _week_to_hour_factors(cls) -> Dict[int, float]:
        """Compound week to day and day to hour factors"""
        return du.combine_dict_list(
            dict_list=[cls._week_to_day_factors(), cls._day_to_hour_factors()],
            operation=operator.mul,
        )

    @classmethod
    def _hour_to_week_factors(cls) -> Dict[int, float]:
        """Compound hour to day and day to week factors"""
        return du.combine_dict_list(
            dict_list=[cls._hour_to_day_factors(), cls._day_to_week_factors()],
            operation=operator.mul,
        )

    @classmethod
    def _hour_to_day_factors(cls) -> Dict[int, float]:
        """Inverse of day to hour factors"""
        return {k: 1 / v for k, v in cls._day_to_hour_factors().items()}

    @classmethod
    def _day_to_week_factors(cls) -> Dict[int, float]:
        """Inverse of week to day factors"""
        return {k: 1 / v for k, v in cls._week_to_day_factors().items()}

    @classmethod
    def _week_to_day_factors(cls) -> Dict[int, float]:
        return {
            1: 0.2,
            2: 0.2,
            3: 0.2,
            4: 0.2,
            5: 1,
            6: 1,
        }

    @classmethod
    def _day_to_hour_factors(cls) -> Dict[int, float]:
        return {
            1: 1/3,
            2: 1/6,
            3: 1/3,
            4: 1/12,
            5: 1/24,
            6: 1/24,
        }

    def get_conversion_factors(self,
                               to_time_format: TimeFormat,
                               ) -> Dict[int, float]:
        """Get the conversion factors for each time period

        Get a dictionary of the values to multiply each time period by
        in order to convert between time formats

        Parameters
        ----------
        to_time_format:
            The time format you want to convert this time format to.
            Cannot be the same TimeFormat as this.

        Returns
        -------
        conversion_factors:
            A dictionary of conversion factors for each time period.
            Keys will the the time period, and values are the conversion
            factors.

        Raises
        ------
        ValueError:
            If any of the given values are invalid, or to_time_format
            is the same TimeFormat as self.
        """
        # Validate inputs
        if not isinstance(to_time_format, TimeFormat):
            raise ValueError(
                "Expected to_time_format to be a TimeFormat object. Got: %s"
                % (type(to_time_format))
            )

        if to_time_format == self:
            raise ValueError(
                "Cannot get the conversion factors when converting to self."
            )

        # Figure out which function to call
        if self == TimeFormat.AVG_WEEK and to_time_format == TimeFormat.AVG_DAY:
            factors_fn = self._week_to_day_factors
        elif self == TimeFormat.AVG_WEEK and to_time_format == TimeFormat.AVG_HOUR:
            factors_fn = self._week_to_hour_factors
        elif self == TimeFormat.AVG_DAY and to_time_format == TimeFormat.AVG_WEEK:
            factors_fn = self._day_to_week_factors
        elif self == TimeFormat.AVG_DAY and to_time_format == TimeFormat.AVG_HOUR:
            factors_fn = self._day_to_hour_factors
        elif self == TimeFormat.AVG_HOUR and to_time_format == TimeFormat.AVG_WEEK:
            factors_fn = self._hour_to_week_factors
        elif self == TimeFormat.AVG_HOUR and to_time_format == TimeFormat.AVG_DAY:
            factors_fn = self._hour_to_day_factors
        else:
            raise nd.NormitsDemandError(
                "Cannot figure out the conversion factors to get from "
                "time_format %s to %s"
                % (self.value, to_time_format.value)
            )

        return factors_fn()


class DVector:
    """One dimensional, segmentation and zoning flexible, heterogeneous data.

    Custom Vector Object for NorMITs Demand
    A Demand Vector object built to handle flexible segmentations and zoning
    systems. All data is stored in "data dictionaries" alongside meta data
    to link all data points back to segments and zones. DVector has been
    multiprocessed and optimised to avoid memory over-utilisation.

    Create a Dvector by passing in a pandas.DataFrame, or a "data
    dictionary".

    WARNING: DVectors can be converted into Pandas.DatFrame, however this is
    not recommended for large DVectors as DataFrames are incredibly inefficient
    for storing DVector data due to the number of repeated values needed in
    a dataframe.

    Attributes
    ----------
    zoning_system:
        The zoning system of this DVector, as passed into the constructor.

    segmentation:
        The segmentation level of this DVector, as passed into the constructor.

    process_count:
        The maximum number of parallel processes that this DVector can use
        when processing data.

    verbose:
        If set to True, the DVector will print out progress updates while
        running. Currently, setting this to True will not change anything.
    """
    # Constants
    __version__ = nd.__version__

    _zone_col = 'zone'
    _segment_col = 'segment'
    _val_col = 'val'
    _zero_infill = 1e-10

    _dvec_suffix = '_dvec%s' % consts.COMPRESSION_SUFFIX

    # Default chunk sizes for multiprocessing
    # Chosen through best guesses and tests
    _chunk_size = 100000
    _to_df_min_chunk_size = 400
    _translate_zoning_min_chunk_size = 700

    # Use for getting a bunch of progress bars for mp code
    _debugging_mp_code = False

    def __init__(self,
                 zoning_system: core.ZoningSystem,
                 segmentation: core.SegmentationLevel,
                 import_data: Union[pd.DataFrame, nd.DVectorData],
                 time_format: Union[str, TimeFormat] = None,
                 zone_col: str = None,
                 val_col: str = None,
                 df_naming_conversion: str = None,
                 df_chunk_size: int = None,
                 infill: Any = 0,
                 process_count: int = consts.PROCESS_COUNT,
                 verbose: bool = False,
                 ) -> None:
        """
        Validates the input arguments and creates a DVector

        Parameters
        ----------
        zoning_system:
            An nd.core.ZoningSystem object defining the zoning system this
            DVector is using.

        segmentation:
            An nd.core.ZoningSystem object defining the zoning system this
            DVector is using.

        import_data:
            The data to become the DVector data. Can take either a
            dictionary in the DVector.data format (usually used internally),
            or a pandas DataFrame.

        time_format:
            The time_format that the import_data represents. Must be one of:
            'avg_week', 'avg_day', 'avg_hour'
            if a tp segment is being used, then this needs to be defined!
            If there is no tp segment, this is an optional argument, but
            it is highly recommended.

        zone_col:
            Only used when import_data is a pandas.DataFrame. This is the name
            of the column in import_data containing the zones names.

        val_col:
            Only used when import_data is a pandas.DataFrame. This is the name
            of the column in import_data containing the data values.

        df_naming_conversion:
            Only used when import_data is a pandas.DataFrame.
            A dictionary mapping segment names in segmentation.naming_order
            into df columns names. e.g.
            {segment_name: column_name}

        df_chunk_size:
            Only used when import_data is a pandas.DataFrame.
            The size of each chunk when processing a large pandas.DataFrame
            into a DVector. Often, the size of the DataFrame can be the
            primary cause of slowdown. By processing in chunks, the conversion
            runs much faster. By default, set to DVector._chunk_size. Play
            with different values to get speedup.

        infill:
            If there are any missing segmentation/zone combinations this value
            will be used to infill. By default, set to 0.

        process_count:
            The number of processes to create in the Pool. Typically this
            should not exceed the number of cores available.
            Negative numbers mean that amount less than all cores e.g. -2
            would be os.cpu_count() - 2. If set to zero, multiprocessing
            will not be used.
            Defaults to consts.PROCESS_COUNT.

        verbose:
            How chatty to be while processing things. Mostly used in debugging
            and should be set to False usually.
        """
        # Validate arguments
        if zoning_system is not None:
            if not isinstance(zoning_system, nd.core.zoning.ZoningSystem):
                raise ValueError(
                    "Given zoning_system is not a nd.core.ZoningSystem object."
                    "Got a %s object instead."
                    % type(zoning_system)
                )

        if not isinstance(segmentation, nd.core.segments.SegmentationLevel):
            raise ValueError(
                "Given segmentation is not a nd.core.SegmentationLevel object."
                "Got a %s object instead."
                % type(segmentation)
            )

        # Init
        self._zoning_system = zoning_system
        self._segmentation = segmentation
        self._time_format = self._validate_time_format(time_format)
        self.verbose = verbose
        self._df_chunk_size = self._chunk_size if df_chunk_size is None else df_chunk_size

        # Define multiprocessing arguments
        self.process_count = process_count

        if self.process_count == 0:
            self._chunk_divider = 1
        else:
            self._chunk_divider = self.process_count * 3

        # Set defaults if args not set
        zone_col = self._zone_col if zone_col is None else zone_col
        val_col = self._val_col if val_col is None else val_col

        # Try to convert the given data into DVector format
        if isinstance(import_data, pd.DataFrame):
            self._data = self._dataframe_to_dvec(
                df=import_data,
                zone_col=zone_col,
                val_col=val_col,
                segment_naming_conversion=df_naming_conversion,
                infill=infill,
            )
        elif isinstance(import_data, dict):
            self._data = self._dict_to_dvec(
                import_data=import_data,
                infill=infill,
            )
        else:
            raise NotImplementedError(
                "Don't know how to deal with anything other than: "
                "pandas DF, or dict"
            )

    # SETTERS AND GETTERS
    @property
    def zoning_system(self):
        return self._zoning_system

    @zoning_system.setter
    def zoning_system(self, a):
        raise DVectorError(
            "Zoning System cannot be changed for an already created DVector."
        )

    @property
    def segmentation(self):
        return self._segmentation

    @segmentation.setter
    def segmentation(self, a):
        raise DVectorError(
            "Segmentation Level cannot be changed for an already created DVector."
        )

    @property
    def process_count(self):
        return self._process_count

    @process_count.setter
    def process_count(self, a):
        if a < 0:
            self._process_count = os.cpu_count() + a
        else:
            self._process_count = a

    @property
    def time_format(self):
        if self._time_format is None:
            return None
        return self._time_format.name

    @classmethod
    def _valid_time_formats(cls) -> List[str]:
        """
        Returns a list of valid strings to pass for time_format
        """
        return [x.value for x in TimeFormat]

    # BUILT IN METHODS
    def __mul__(self: DVector, other: DVector) -> DVector:
        """
        Builds a new Dvec by multiplying a and b together.

        How to join the two Dvectors is defined by the segmentation of each
        Dvector.

        Retains process_count, df_chunk_size, and verbose params from a.

        Parameters
        ----------
        self:
            The first DVector to multiply

        other:
            The second DVector to multiply

        Returns
        -------
        c:
            A new DVector which is the product of multiplying a and b.
        """
        # We can only multiply against other DVectors
        if not isinstance(other, DVector):
            raise nd.NormitsDemandError(
                "The __mul__ operator can only be used with."
                "a DVector objects on each side. Got %s and %s."
                % (type(self), type(other))
            )

        # ## CHECK WE CAN MULTIPLY a AND b ## #
        if self.zoning_system == other.zoning_system:
            return_zoning_system = self.zoning_system
        elif self.zoning_system is None:
            return_zoning_system = other.zoning_system
        elif other.zoning_system is None:
            return_zoning_system = self.zoning_system
        else:
            raise nd.ZoningError(
                "Cannot multiply two Dvectors using different zoning systems.\n"
                "zoning system of a: %s\n"
                "zoning system of b: %s\n"
                % (self.zoning_system.name, other.zoning_system.name)
            )

        # ## DO MULTIPLICATION ## #
        # Use the segmentations to figure out what to multiply
        multiply_dict, return_segmentation = self.segmentation * other.segmentation

        # Build the dvec data here with multiplication
        dvec_data = dict.fromkeys(multiply_dict.keys())
        for final_seg, (self_key, other_key) in multiply_dict.items():
            dvec_data[final_seg] = self._data[self_key] * other._data[other_key]

        return DVector(
            zoning_system=return_zoning_system,
            segmentation=return_segmentation,
            time_format=self._choose_time_format(other),
            import_data=dvec_data,
            process_count=self.process_count,
            verbose=self.verbose,
        )

    def copy(self) -> DVector:
        """Returns a copy of this class"""
        return DVector(
            zoning_system=self.zoning_system,
            segmentation=self.segmentation,
            time_format=self._time_format,
            import_data=self._data,
            process_count=self.process_count,
            verbose=self.verbose,
        )

    # CUSTOM METHODS
    def _validate_time_format(self,
                              time_format: Union[str, TimeFormat],
                              ) -> TimeFormat:
        """Validate the time format is a valid value

        Parameters
        ----------
        time_format:
            The name of the time format name to validate

        Returns
        -------
        time_format:
            Returns a tidied up version of the passed in time_format.

        Raises
        ------
        ValueError:
            If the given time_format is not on of self._valid_time_formats
        """
        # Time period format only matters if it's in the segmentation
        if self.segmentation.has_time_period_segments():
            if time_format is None:
                raise ValueError(
                    "The given segmentation level has time periods in its "
                    "segmentation, but the format of this time period has "
                    "not been defined.\n"
                    "\tTime periods segment name: %s\n"
                    "\tValid time_format values: %s"
                    % (self.segmentation._time_period_segment_name,
                       self._valid_time_formats(),
                       )
                )

        # If None or TimeFormat, that's fine
        if time_format is None or isinstance(time_format, TimeFormat):
            return time_format

        # Check we've got a valid value
        time_format = time_format.strip().lower()
        if time_format not in self._valid_time_formats():
            raise ValueError(
                "The given time_format is not valid.\n"
                "\tGot: %s\n"
                "\tExpected one of: %s"
                % (time_format, self._valid_time_formats())
            )

        # Convert into a TimeFormat constant
        return_val = None
        for name, time_format_obj in TimeFormat.__members__.items():
            if name.lower() == time_format:
                return_val = time_format_obj
                break

        if return_val is None:
            raise ValueError(
                "We checked that the given time_format was valid, but it "
                "wasn't set when we tried to set it. This shouldn't be "
                "possible!"
            )

        return return_val

    def _choose_time_format(self, other: DVector) -> TimeFormat:
        """Returns the time_format to use from self and other

        Internal function for use when combining multiple DVectors.
        Will choose self.time_format if it is not None, otherwise
        other.time_format will be returned.
        If neither is set, then None is returned

        Parameters
        ----------
        other:
            The other DVector to choose a time_format from.

        Returns
        -------
        time_format:
            The time format to retain from self and other

        Raises
        ------
        Warning:
            If both self and other have a time_format set and they are not
            the same
        """
        # If both are set, but not the same
        if self._time_format is not None and other._time_format is not None:
            if self._time_format != other._time_format:
                warnings.warn(
                    "The time_format of both DVectors is set, but they are not "
                    "set to the same format. This might not give the "
                    "results you expect!\n"
                    "\tself time_format: %s\n"
                    "\tother time_format: %s"
                    % (self.time_format, other.time_format)
                )

        if self._time_format is not None:
            return self._time_format

        if other._time_format is not None:
            return other._time_format

        return None

    def _dict_to_dvec(self,
                      import_data: nd.DVectorData,
                      infill: Any
                      ) -> nd.DVectorData:
        """
        Validates a given DVector.data dictionary.

        This function should only really be used by the __init__ when
        class functions are creating new DVector dictionaries.

        While converting, will:
        - Makes sure that all segments in the dictionary are valid segments
          for this DVector's segmentation
        - Makes sure that the given dictionary contains ONLY valid segments.
          An error is raised if any extra segments are in the dictionary.
        """
        # TODO(BT): Make sure all values are the correct size of the zoning
        #  system
        # Init

        # ## MAKE SURE DATA CONTAINS ALL SEGMENTS ##
        # Figure out what the default value should be
        if self.zoning_system is None:
            default_val = infill
        else:
            default_val = np.array([infill] * self.zoning_system.n_zones)

        # Find the missing segments and infill
        not_in = set(self.segmentation.segment_names) - import_data.keys()
        for name in not_in:
            import_data[name] = default_val.copy()

        # Double check that all segment names are valid
        if not self.segmentation.is_correct_naming(list(import_data.keys())):
            raise core.SegmentationError(
                "There are additional segment names in the given DVector data "
                "dictionary. Additional names: %s"
                % (set(import_data.keys()) - self.segmentation.segment_names)
            )

        # Make sure all values are the correct shape?
        # Probably want to multiprocess this for large values!
        # Is it worth adding an option to skip some of these steps if
        #  the dictionary is given from a internal DVector build?
        #  We can trust these conditions are met in this environment.
        return import_data

    def _dataframe_to_dvec_internal(self,
                                    df_chunk,
                                    ) -> nd.DVectorData:
        """
        The internal function of _dataframe_to_dvec - for multiprocessing
        """
        if self.zoning_system is None:
            # Make sure only one value exists for each segment
            segments = df_chunk[self._segment_col].tolist()
            segment_set = set(segments)
            if len(segment_set) != len(segments):
                raise ValueError(
                    "The given DataFrame has one or more repeated values "
                    "for some of the segments. Found %s segments, but only "
                    "%s of them are unique."
                    % (len(segments), len(segment_set))
                )

            # Can use 1 to 1 connection to speed this up
            vals = df_chunk[self._val_col].to_list()
            dvec_chunk = {s: v for s, v, in zip(segments, vals)}

        else:
            # Generate the data on a per segment basis
            dvec_chunk = dict.fromkeys(df_chunk[self._segment_col].tolist())

            for segment in df_chunk['segment'].unique():
                # Get all available pop for this segment
                seg_data = df_chunk[df_chunk[self._segment_col] == segment].copy()

                # Check that it's a valid segment_name
                if segment not in self.segmentation.segment_names:
                    raise ValueError(
                        "%s is not a valid segment name for a Dvector using %s "
                        "segmentation.\n Data with segment:\n%s"
                        % (segment, self.segmentation.name, seg_data)
                    )


                # TODO(BT): There's a VERY slight chance that duplicate zones
                #  could be split across processes. Need to add a check for
                #  this on the calling function.
                # Make sure there are no duplicate zones
                seg_zones = seg_data[self._zone_col].tolist()
                seg_zones_set = set(seg_zones)
                if len(seg_zones_set) != len(seg_zones):
                    raise ValueError(
                        "The given DataFrame has one or more repeated values "
                        "for some of the zones in segment %s. Found %s "
                        "segments, but only %s of them are unique."
                        % (segment, len(seg_zones), len(seg_zones_set))
                    )

                # Make sure zones that don't exist in this zoning system are found
                zoning_system_zones = set(self.zoning_system.unique_zones)
                extra_zones = seg_zones_set - zoning_system_zones
                if len(extra_zones) > 0:
                    raise ValueError(
                        "Found zones that don't exist in %s zoning in the "
                        "given DataFrame. For segment %s, the following "
                        "zones do not belong to this zoning system:\n%s"
                        % (self.zoning_system.name, segment, extra_zones)
                    )

                # Filter down to just data as values, and zoning system as the index
                seg_data = seg_data.reindex(columns=[self._zone_col, self._val_col])
                seg_data = seg_data.set_index(self._zone_col)

                # Infill any missing zones as 0
                seg_data = seg_data.reindex(self.zoning_system.unique_zones, fill_value=0)
                dvec_chunk[segment] = seg_data.values.flatten()

        return dvec_chunk

    def _dataframe_to_dvec(self,
                           df: pd.DataFrame,
                           zone_col: str,
                           val_col: str,
                           segment_naming_conversion: str,
                           infill: Any,
                           ) -> nd.DVectorData:
        """
        Converts a pandas dataframe into dvec.data internal structure

        While converting, will:
        - Make sure that any missing segment/zone combinations are infilled
          with infill
        - Make sure only one value exist for each segment/zone combination
        """
        # Init columns depending on if we have zones
        required_cols = self.segmentation.naming_order + [self._val_col]
        sort_cols = [self._segment_col]

        # Add zoning if we need it
        if self.zoning_system is not None:
            required_cols += [self._zone_col]
            sort_cols += [self._zone_col]

        # ## VALIDATE AND CONVERT THE GIVEN DATAFRAME ## #
        # Rename import_data columns to internal names
        rename_dict = {zone_col: self._zone_col, val_col: self._val_col}
        df = df.rename(columns=rename_dict)

        # Rename the segment columns if needed
        if segment_naming_conversion is not None:
            df = self.segmentation.rename_segment_cols(df, segment_naming_conversion)

        # Make sure we don't have any extra columns
        extra_cols = set(list(df)) - set(required_cols)
        if len(extra_cols) > 0:
            raise ValueError(
                "Found extra columns in the given DataFrame than needed. The "
                "given DataFrame should only contain val_col, "
                "segmentation_cols, and the zone_col (where applicable). "
                "Found the following extra columns: %s"
                % extra_cols
            )

        # Add the segment column - drop the individual cols
        df[self._segment_col] = self.segmentation.create_segment_col(
            df=df,
            naming_conversion=segment_naming_conversion
        )
        df = df.drop(columns=self.segmentation.naming_order)

        # Sort by the segment columns for MP speed
        df = df.sort_values(by=sort_cols)

        # ## MULTIPROCESSING SETUP ## #
        # If the dataframe is smaller than the chunk size, evenly split across cores
        if len(df) < self._df_chunk_size * self.process_count:
            chunk_size = math.ceil(len(df) / self.process_count)
        else:
            chunk_size = self._df_chunk_size

        # setup a pbar
        pbar_kwargs = {
            'desc': "Converting df to dvec",
            'unit': "segment",
            'disable': (not self._debugging_mp_code),
            'total': math.ceil(len(df) / chunk_size),
        }

        # ## MULTIPROCESS THE DATA CONVERSION ## #
        # Build a list of arguments
        kwarg_list = list()
        for df_chunk in pd_utils.chunk_df(df, chunk_size):
            kwarg_list.append({'df_chunk': df_chunk})

        # Call across multiple threads
        data_chunks = multiprocessing.multiprocess(
            fn=self._dataframe_to_dvec_internal,
            kwargs=kwarg_list,
            process_count=self.process_count,
            pbar_kwargs=pbar_kwargs,
        )
        data = du.sum_dict_list(data_chunks)

        # ## MAKE SURE DATA CONTAINS ALL SEGMENTS ##
        # find the segments which arent in there
        not_in = set(self.segmentation.segment_names) - data.keys()

        # Figure out what the default value should be
        if self.zoning_system is None:
            default_val = infill
        else:
            default_val = np.array([infill] * self.zoning_system.n_zones)

        # Infill the missing segments
        for name in not_in:
            data[name] = copy.copy(default_val)

        return data

    def get_segment_data(self,
                         segment_name: str = None,
                         segment_dict: Dict[str, Any] = None,
                         ) -> Union[np.array, int, float]:
        """
        Gets the data for the given segment from the Dvector

        If no data for the given segment exists, then returns a np.array
        of the length of this DVectors zoning system.

        Parameters
        ----------
        segment_name:
            The name of the segment to get. Can only set either this or
            segment_dict. 
        
        segment_dict:
            A dictionary of a segment to get. Should be as
            {segment_name: segment_value} pairs.
            Can only set this or segment_name. If this value is set, it will
            internally be converted into a segment_name.

        Returns
        -------
        segment_data:
            The data for segment_name

        """
        # Make sure only one argument is set
        if not du.xor(segment_name is None, segment_dict is None):
            raise ValueError(
                "Need to set either segment_name or segment_dict in order to "
                "get the data of a segment.\n"
                "Both values cannot be set, neither can both be left as None."
            )

        # Build the segment_name if we don't have it
        if segment_dict is not None:
            raise NotImplemented(
                "Need to write code to convert a segment_dict into a valid "
                "segment_name"
            )

        if segment_name not in self.segmentation.segment_names:
            raise ValueError(
                "%s is not a valid segment name for a Dvector using %s "
                "segmentation." % self.segmentation.name
            )

        # Get data and covert to zoning system
        return self._data[segment_name]

    @staticmethod
    def _to_df_internal(self_data: nd.DVectorData,
                        self_zoning_system: core.ZoningSystem,
                        self_segmentation: core.SegmentationLevel,
                        col_names: List[str],
                        val_col: str,
                        zone_col: str,
                        ) -> pd.DataFrame:
        """
        Internal function of self.to_df(). For multiprocessing
        """
        # Init
        index_cols = du.list_safe_remove(col_names, [val_col])
        concat_ph = list()

        # Convert all given data into dataframes
        for segment_name, data in self_data.items():
            # Add the zoning system back in
            if self_zoning_system is None:
                df = pd.DataFrame([{val_col: data}])
            else:
                index = pd.Index(self_zoning_system.unique_zones, name=zone_col)
                data = {val_col: data.flatten()}
                df = pd.DataFrame(index=index, data=data).reset_index()

            # Add all segments into the df
            seg_dict = self_segmentation.get_seg_dict(segment_name)
            for col_name, col_val in seg_dict.items():
                df[col_name] = col_val

            # Make sure all dfs are in the same format
            df = df.reindex(columns=col_names)
            concat_ph.append(df)

        return pd.concat(concat_ph, ignore_index=True)

    def to_df(self) -> pd.DataFrame:
        """
        Convert this DVector into a pandas dataframe with the segmentation
        as the index
        """
        # Init
        col_names = list(self.segmentation.get_seg_dict(list(self._data.keys())[0]).keys())
        col_names = col_names + [self._val_col]
        if self.zoning_system is not None:
            zone_col = self.zoning_system.col_name
            col_names = [zone_col] + col_names
        else:
            zone_col = None

        # ## MULTIPROCESS ## #
        # Define chunk size
        total = len(self._data)
        chunk_size = math.ceil(total / self._chunk_divider)

        # Make sure the chunks aren't too small
        if chunk_size < self._to_df_min_chunk_size:
            chunk_size = self._to_df_min_chunk_size

        # Define the kwargs
        kwarg_list = list()
        for keys_chunk in du.chunk_list(self._data.keys(), chunk_size):
            # Calculate subsets of self.data to avoid locks between processes
            self_data_subset = {k: self._data[k] for k in keys_chunk}

            if self.zoning_system is not None:
                self_zoning_system = self.zoning_system.copy()
            else:
                self_zoning_system = None

            # Assign to a process
            kwarg_list.append({
                'self_data': self_data_subset,
                'self_zoning_system': self_zoning_system,
                'self_segmentation': self.segmentation.copy(),
                'col_names': col_names.copy(),
                'val_col': self._val_col,
                'zone_col': zone_col,
            })

        # Define pbar
        pbar_kwargs = {
            'desc': "Converting DVector to dataframe",
            'disable': not self._debugging_mp_code,
        }

        # Run across processes
        dataframe_chunks = multiprocessing.multiprocess(
            fn=self._to_df_internal,
            kwargs=kwarg_list,
            process_count=self.process_count,
            pbar_kwargs=pbar_kwargs,
        )

        # Join all the dataframe chunks together
        return pd.concat(dataframe_chunks, ignore_index=True)

    def compress_out(self, path: nd.PathLike) -> pathlib.Path:
        """
        Writes this DVector to disk at path.

        Parameters
        ----------
        path:
            The path to write this object out to.
            Conventionally should end in .dvec.pbz2.
            If it does not, the suffix will be added, and the new
            path returned.

        Returns
        -------
        path:
            The path that this object was written out to. If path ended in the
            correct suffix, then this will be exactly the same as input path.

        Raises
        ------
        IOError:
            If the path cannot be found.
        """
        # Init
        path = file_ops.cast_to_pathlib_path(path)

        if path.suffix != self._dvec_suffix:
            path = path.parent / (path.stem + self._dvec_suffix)

        return compress.write_out(self, path, overwrite_suffix=False)

    def to_pickle(self, path: nd.PathLike) -> None:
        """
        Pickle (serialize) object to file.

        Parameters
        ----------
        path:
            Filepath to store the pickled object

        """
        with open(path, 'wb') as f:
            pickle.dump(self, f)

    @staticmethod
    def _multiply_and_aggregate_internal(aggregation_keys_chunk,
                                         aggregation_dict,
                                         multiply_dict,
                                         self_data,
                                         other_data,
                                         ):
        """
        Internal function of self.multiply_and_aggregate. For multiprocessing
        """
        # Init
        dvec_data = dict.fromkeys(aggregation_keys_chunk)

        # Multiply and aggregate in chunks
        for out_seg_name in aggregation_keys_chunk:
            # Calculate all the segments to aggregate
            inter_segs = list()
            for segment_name in aggregation_dict[out_seg_name]:
                self_key, other_key = multiply_dict[segment_name]
                result = (self_data[self_key] * other_data[other_key]).flatten()
                inter_segs.append(result)

            # Aggregate!
            dvec_data[out_seg_name] = np.sum(inter_segs, axis=0)

        return dvec_data

    def aggregate(self,
                  out_segmentation: core.SegmentationLevel,
                  split_tfntt_segmentation: bool = False,
                  ) -> DVector:
        """
        Aggregates (by summing) this Dvector into out_segmentation.

        A definition of how to aggregate from self.segmentation to
        out_segmentation must exist, otherwise a SegmentationError will be
        thrown

        Parameters
        ----------
        out_segmentation:
            The segmentation to aggregate into.

        split_tfntt_segmentation:
            If converting from the current segmentation to out_segmentation
            requires the splitting of the tfn_tt segmentation, mark this as
            True - a special type of aggregation is needed underneath.

        Returns
        -------
        aggregated_DVector:
            a new dvector containing the same data, but aggregated to
            out_segmentation.
        """
        # Validate inputs
        if not isinstance(out_segmentation, nd.core.segments.SegmentationLevel):
            raise ValueError(
                "out_segmentation is not the correct type. "
                "Expected SegmentationLevel, got %s"
                % type(out_segmentation)
            )

        # Get the aggregation dict
        if split_tfntt_segmentation:
            aggregation_dict = self.segmentation.split_tfntt_segmentation(out_segmentation)
        else:
            aggregation_dict = self.segmentation.aggregate(out_segmentation)

        # Aggregate!
        # TODO(BT): Add optional multiprocessing if aggregation_dict is big enough
        dvec_data = dict.fromkeys(aggregation_dict.keys())
        for out_seg_name, in_seg_names in aggregation_dict.items():
            in_lst = [self._data[x].flatten() for x in in_seg_names]
            dvec_data[out_seg_name] = np.sum(in_lst, axis=0)

        return DVector(
            zoning_system=self.zoning_system,
            segmentation=out_segmentation,
            time_format=self.time_format,
            import_data=dvec_data,
            process_count=self.process_count,
            verbose=self.verbose,
        )

    def multiply_and_aggregate(self: DVector,
                               other: DVector,
                               out_segmentation: core.SegmentationLevel,
                               ) -> DVector:
        """
        Multiply with other DVector, and aggregates as it goes.

        Useful when the output segmentation of multiplying self and other
        would be massive. Multiplication is done in chunks, and aggregated in
        out_segmentation periodically.

        Parameters
        ----------
        other:
            The DVector to multiply self with.

        out_segmentation:
            The segmentation to use in the outputs DVector

        Returns
        -------
        DVector:
            The result of (self * other).aggregate(out_segmentation)
        """
        # Validate inputs
        if not isinstance(other, DVector):
            raise ValueError(
                "b is not the correct type. Expected Dvector, got %s"
                % type(other)
            )

        if not isinstance(out_segmentation, core.SegmentationLevel):
            raise ValueError(
                "out_segmentation is not the correct type. "
                "Expected SegmentationLevel, got %s"
                % type(out_segmentation)
            )

        # Get the aggregation and multiplication dictionaries
        multiply_dict, mult_return_seg = self.segmentation * other.segmentation
        aggregation_dict = mult_return_seg.aggregate(out_segmentation)

        # ## MULTIPROCESS ## #
        # Define the chunk size
        total = len(aggregation_dict)
        chunk_size = math.ceil(total / self._chunk_divider)

        # Define the kwargs
        kwarg_list = list()
        for keys_chunk in du.chunk_list(aggregation_dict.keys(), chunk_size):
            # Calculate subsets of keys to avoid locks between processes
            agg_dict_subset = {k: aggregation_dict[k] for k in keys_chunk}
            
            key_subset = itertools.chain.from_iterable(agg_dict_subset.values())
            mult_dict_subset = {k: multiply_dict[k] for k in key_subset}

            self_keys, other_keys = zip(*mult_dict_subset.values())
            self_data = {k: self._data[k] for k in self_keys}
            other_data = {k: other._data[k] for k in other_keys}

            # Assign to a process
            kwarg_list.append({
                'aggregation_keys_chunk': keys_chunk,
                'aggregation_dict': agg_dict_subset,
                'multiply_dict': mult_dict_subset,
                'self_data': self_data,
                'other_data': other_data,
            })

        # Define pbar
        pbar_kwargs = {
            'desc': "Multiplying and aggregating",
            'disable': not self._debugging_mp_code,
        }

        # Run across processes
        data_chunks = multiprocessing.multiprocess(
            fn=self._multiply_and_aggregate_internal,
            kwargs=kwarg_list,
            process_count=self.process_count,
            pbar_kwargs=pbar_kwargs,
        )

        # Combine all computation chunks into one
        dvec_data = dict.fromkeys(aggregation_dict.keys())
        for chunk in data_chunks:
            dvec_data.update(chunk)

        return DVector(
            zoning_system=self.zoning_system,
            segmentation=out_segmentation,
            time_format=self._choose_time_format(other),
            import_data=dvec_data,
            process_count=self.process_count,
            verbose=self.verbose,
        )

    def sum_is_close(self,
                     other: nd.DVector,
                     rel_tol: float = 0.0001,
                     abs_tol: float = 0.0,
                     ) -> bool:
        """Checks if the sum() of other is similar to sum() of self

        Whether or not two values are considered close is determined
        according to given absolute and relative tolerances.

        Parameters
        -----------
        other:
            The DVector to check is we are close to

        rel_tol:
            the relative tolerance – it is the maximum allowed difference
            between the sum of pure_attractions and fully_segmented_attractions,
            relative to the larger absolute value of pure_attractions or
            fully_segmented_attractions. By default, this is set to 0.0001,
            meaning the values must be within 0.01% of each other.

        abs_tol:
            is the minimum absolute tolerance – useful for comparisons near
            zero. abs_tol must be at least zero.

        Returns
        -------
        is_close:
             Return True if self.sum() and other.sum() are close to each
             other, False otherwise.
        """
        return math.isclose(
            self.sum(),
            other.sum(),
            rel_tol=rel_tol,
            abs_tol=abs_tol,
        )

    def sum(self) -> float:
        """
        Sums all values within the Dvector and returns the total

        Returns
        -------
        sum:
            The total sum of all values
        """
        return np.sum([x.flatten() for x in self._data.values()])

    @staticmethod
    def _translate_zoning_internal(self_data: nd.DVectorData,
                                   translation: np.array,
                                   ) -> nd.DVectorData:
        """
        Internal function of self.translate_zoning. For multiprocessing
        """
        # Init
        dvec_data = dict.fromkeys(self_data.keys())

        # Translate zoning in chunks
        for key, value in self_data.items():
            value = value.flatten()
            temp = np.broadcast_to(np.expand_dims(value, axis=1), translation.shape)
            temp = temp * translation
            dvec_data[key] = temp.sum(axis=0)

        return dvec_data

    def translate_zoning(self,
                         new_zoning: core.ZoningSystem,
                         weighting: str = None,
                         ) -> DVector:
        """
        Translates this DVector into another zoning system and returns a new
        DVector.

        Parameters
        ----------
        new_zoning:
            The zoning system to translate into.

        weighting:
            The weighting to use when building the translation. Must be None,
            or one of ZoningSystem.possible_weightings

        Returns
        -------
        translated_dvector:
            This DVector translated into new_new_zoning zoning system

        """
        # Validate inputs
        if not isinstance(new_zoning, core.ZoningSystem):
            raise ValueError(
                "new_zoning is not the correct type. "
                "Expected ZoningSystem, got %s"
                % type(new_zoning)
            )

        if self.zoning_system.name is None:
            raise nd.NormitsDemandError(
                "Cannot translate the zoning system of a DVector that does "
                "not have a zoning system to begin with."
            )

        # Get translation
        translation = self.zoning_system.translate(new_zoning, weighting)

        # ## MULTIPROCESS ## #
        # Define the chunk size
        total = len(self._data)
        chunk_size = math.ceil(total / self._chunk_divider)

        # Make sure the chunks aren't too small
        if chunk_size < self._translate_zoning_min_chunk_size:
            chunk_size = self._translate_zoning_min_chunk_size

        # Define the kwargs
        kwarg_list = list()
        for keys_chunk in du.chunk_list(self._data.keys(), chunk_size):
            # Calculate subsets of self.data to avoid locks between processes
            self_data_subset = {k: self._data[k] for k in keys_chunk}

            # Assign to a process
            kwarg_list.append({
                'self_data': self_data_subset,
                'translation': translation.copy(),
            })

        # Define pbar
        pbar_kwargs = {
            'desc': "Translating",
            'disable': not self._debugging_mp_code,
        }

        # Run across processes
        data_chunks = multiprocessing.multiprocess(
            fn=self._translate_zoning_internal,
            kwargs=kwarg_list,
            process_count=self.process_count,
            pbar_kwargs=pbar_kwargs,
        )

        # Combine all computation chunks into one
        dvec_data = dict.fromkeys(self._data.keys())
        for chunk in data_chunks:
            dvec_data.update(chunk)

        return DVector(
            zoning_system=new_zoning,
            segmentation=self.segmentation,
            time_format=self.time_format,
            import_data=dvec_data,
            process_count=self.process_count,
            verbose=self.verbose,
        )

    def expand_segmentation(self,
                            expansion_dvec: DVector,
                            ) -> DVector:
        """Expands the segmentation of self to include expansion_dvec

        Expansion definition is defined in the expand.csv, path to this file
        can be found in SegmentationLevel._expand_definitions_path

        Parameters
        ----------
        expansion_dvec:
            Values should be the weights to give to each segment, or
            zone + segment (if using zoning system).

        Returns
        -------
        expanded_dvec:
            A new DVector instance that has been expanded with expansion_dvec.

        Raises
        ------
        ValueError:
            If the given values are not the correct types, or there are
            different area types being used between self and expansion_dvec.
        """
        # Validate inputs
        if not isinstance(expansion_dvec, DVector):
            raise ValueError(
                "expansion_dvec is not the correct type. "
                "Expected DVector, got %s"
                % type(expansion_dvec)
            )

        # Make sure the expansion DVector is in the right zoning system
        if self.zoning_system is not None:
            if expansion_dvec.zoning_system not in [self.zoning_system, None]:
                raise ValueError(
                    "Cannot expand the segmentation of a DVector using an "
                    "expansion DVector with a different zoning system. "
                    "Expected either this zoning system (%s), or no zoning "
                    "system. Got: %s"
                    % (self.zoning_system, expansion_dvec.zoning_system)
                )

        # ## EXPAND ## #
        expand_dict, return_seg = self.segmentation.expand(expansion_dvec.segmentation)

        # Build the new DVec data from the expansion
        dvec_data = dict.fromkeys(expand_dict.keys())
        for final_seg, (self_key, other_key) in expand_dict.items():
            dvec_data[final_seg] = self._data[self_key] * expansion_dvec._data[other_key]

        expanded_dvec = DVector(
            zoning_system=self.zoning_system,
            segmentation=return_seg,
            import_data=dvec_data,
            process_count=self.process_count,
            verbose=self.verbose,
        )

        # Make sure we're not dropping any demand
        if not self.sum_is_close(expanded_dvec):
            raise ValueError(
                "Error when expanding DVector. Before and after totals do "
                "not match\n"
                "Before: %f\n"
                "After:  %f"
                % (self.sum(), expanded_dvec.sum())
            )

        return expanded_dvec

    def subset(self,
               out_segmentation: nd.core.SegmentationLevel,
               ) -> DVector:
        """Subsets the segmentation of self to out_segmentation

        Subset definition is defined in the subset.csv, path to this file
        can be found in SegmentationLevel._subset_definitions_path

        Parameters
        ----------
        out_segmentation:
            A SegmentationLevel object defining the segmentation level to
            subset this DVectors segmentation down to.

        Returns
        -------
        subset_dvec:
            A new DVector instance that has been subset to out_segmentation.

        Raises
        ------
        ValueError:
            If the given values are not the correct types, or there are
            different area types being used between self and expansion_dvec.
        """
        # Validate inputs
        if not isinstance(out_segmentation, nd.core.segments.SegmentationLevel):
            raise ValueError(
                "target_segmentation is not the correct type. "
                "Expected SegmentationLevel, got %s"
                % type(out_segmentation)
            )

        # Get the subset definition
        subset_list = self.segmentation.subset(out_segmentation)

        # Keep just the subset
        dvec_data = dict.fromkeys(subset_list)
        for segment in subset_list:
            dvec_data[segment] = self._data[segment]

        return DVector(
            zoning_system=self.zoning_system,
            segmentation=out_segmentation,
            time_format=self.time_format,
            import_data=dvec_data,
            process_count=self.process_count,
            verbose=self.verbose,
        )

    def split_tfntt_segmentation(self,
                                 out_segmentation: core.SegmentationLevel
                                 ) -> DVector:
        """
        Converts a DVector with p and tfn_tt segmentation in out_segmentation

        Splits the tfn_tt segment into it's components and aggregates up to
        out_segmentation. The DVector needs to be using a segmentation that
        contains tfn_tt and p in order for this to work.

        Parameters
        ----------
        out_segmentation:
            The segmentation level the output vector should be in.

        Returns
        -------
        new_dvector:
            A new DVector containing the same data but in using
            out_segmentation as its segmentation level.

        Raises
        ------
        SegmentationError:
            If the DVector does not contain both tfn_tt and p segments.
        """
        # Validate inputs
        if not isinstance(out_segmentation, core.SegmentationLevel):
            raise ValueError(
                "out_segmentation is not the correct type. "
                "Expected SegmentationLevel, got %s"
                % type(out_segmentation)
            )

        if 'tfn_tt' not in self.segmentation.naming_order:
            raise nd.SegmentationError(
                "This Dvector is not using a segmentation which contains "
                "'tfn_tt'. Need to be using a segmentation using both 'tfn_tt' "
                "and 'p' segments in order to split tfn_tt."
                "Current segmentation uses: %s"
                % self.segmentation.naming_order
            )

        if 'p' not in self.segmentation.naming_order:
            raise nd.SegmentationError(
                "This Dvector is not using a segmentation which contains "
                "'p'. Need to be using a segmentation using both 'tfn_tt' "
                "and 'p' segments in order to split tfn_tt."
                "Current segmentation uses: %s"
                % self.segmentation.naming_order
            )

        # Get the aggregation dict
        aggregation_dict = self.segmentation.split_tfntt_segmentation(out_segmentation)

        # Aggregate!
        # TODO(BT): Add optional multiprocessing if aggregation_dict is big enough
        dvec_data = dict()
        for out_seg_name, in_seg_names in aggregation_dict.items():
            in_lst = [self._data[x].flatten() for x in in_seg_names]
            dvec_data[out_seg_name] = np.sum(in_lst, axis=0)

        return DVector(
            zoning_system=self.zoning_system,
            segmentation=out_segmentation,
            time_format=self.time_format,
            import_data=dvec_data,
            process_count=self.process_count,
            verbose=self.verbose,
        )

    def split_segmentation_like(self,
                                other: DVector,
                                ) -> DVector:
        """
        Splits this DVector segmentation into other.segmentation

        Using other to derive the splitting factors, this DVector is split
        into the same segmentation as other.segmentation. Splits are derived
        as an average across all zones for each segment in other, resulting in
        a single splitting factor for each segment. The splitting factor is
        then applied to this DVector, equally across all zones.

        Parameters
        ----------
        other:
            The DVector to use to determine the segmentation to split in to,
            as well as the weights to use for the splits.

        Returns
        -------
        new_dvector:
            A new DVector containing the same total as values, but split
            into other.segmentation segmentation.

        Raises
        ------
        ValueError:
            If the given parameters are not the correct types

        SegmentationError:
            If the segmentation cannot be split. This DVector must be
            in a segmentation that is a subset of other.segmentation.
        """
        # Validate inputs
        if not isinstance(other, DVector):
            raise ValueError(
                "other is not the correct type. "
                "Expected DVector, got %s"
                % type(other)
            )

        # Get the dictionary defining how to split
        split_dict = self.segmentation.split(other.segmentation)

        # Split!
        # TODO(BT): Add optional multiprocessing if split_dict is big enough
        dvec_data = dict.fromkeys(other.segmentation.segment_names)
        for in_seg_name, out_seg_names in split_dict.items():
            # Calculate the splitting factors
            other_segs = [np.mean(other._data[s]) for s in out_seg_names]
            split_factors = other_segs / np.sum(other_segs)

            # Get the original value
            self_seg = self._data[in_seg_name]

            # Split
            for name, factor in zip(out_seg_names, split_factors):
                dvec_data[name] = self_seg * factor

        return DVector(
            zoning_system=self.zoning_system,
            segmentation=other.segmentation,
            time_format=self._choose_time_format(other),
            import_data=dvec_data,
            process_count=self.process_count,
            verbose=self.verbose,
        )

    def balance_at_segments(self,
                            other: DVector,
                            split_weekday_weekend: bool = False,
                            ) -> DVector:
        """
        Balance segment totals to other, ignoring zoning splits.

        Essentially does
        self[segment] *= other[segment].sum() / self[segment].sum()
        for all segments.

        Parameters
        ----------
        other:
            The DVector to control this one to. Must have the same segmentation
            as this DVector

        split_weekday_weekend:
            Whether to control the time periods as weekday and weekend splits
            instead of each individual time period. If set to True,
            each DVector must be at a segmentation with a 'tp' segment.

        Returns
        -------
        controlled_dvector:
            A copy of this DVector, controlled to other. The total of each
            segment should be equal across self and other.

        Raises
        ------
        ValueError:
            If the given parameters are not the correct types.

        ValueError:
            If self and other do not have the same segmentation.
        """
        # Init
        infill = self._zero_infill

        # Validate inputs
        if not isinstance(other, DVector):
            raise ValueError(
                "other is not the correct type. "
                "Expected DVector, got %s"
                % type(other)
            )

        if self.segmentation.name != other.segmentation.name:
            raise ValueError(
                "Segmentation of both DVectors does not match! "
                "Perhaps you need to call "
                "self.split_segmentation_like(other) to bring them into "
                "alignment?\n"
                "self segmentation: %s\n"
                "other segmentation: %s"
                % (self.segmentation.name, other.segmentation.name)
            )

        # Loop through each segment and control
        dvec_data = dict.fromkeys(self.segmentation.segment_names)

        if split_weekday_weekend:
            # Get the grouped segment lists
            wk_day_segs = self.segmentation.get_grouped_weekday_segments()
            wk_end_segs = self.segmentation.get_grouped_weekend_segments()

            # Control by weekday and weekend separately
            for split_wk_segs in [wk_day_segs, wk_end_segs]:
                for segment_group in split_wk_segs:
                    # Get data and infill zeros
                    self_data_lst = list()
                    other_data_lst = list()
                    for segment in segment_group:
                        # Get data
                        self_data = self._data[segment]
                        other_data = other._data[segment]

                        # Infill zeros
                        self_data = np.where(self_data <= 0, infill, self_data)
                        other_data = np.where(other_data <= 0, infill, other_data)

                        # Append
                        self_data_lst.append(self_data)
                        other_data_lst.append(other_data)

                    # Get the control factor
                    factor = np.sum(other_data_lst) / np.sum(self_data_lst)

                    # Balance each segment
                    for segment, self_data in zip(segment_group, self_data_lst):
                        dvec_data[segment] = self_data * factor

        else:
            # Control all segments as normal
            for segment in self.segmentation.segment_names:
                # Get data
                self_data = self._data[segment]
                other_data = other._data[segment]

                # Infill zeros
                self_data = np.where(self_data <= 0, infill, self_data)
                other_data = np.where(other_data <= 0, infill, other_data)

                # Balance
                dvec_data[segment] = self_data * (np.sum(other_data) / np.sum(self_data))

        return DVector(
            zoning_system=self.zoning_system,
            segmentation=other.segmentation,
            time_format=self.time_format,
            import_data=dvec_data,
            process_count=self.process_count,
            verbose=self.verbose,
        )

    def sum_zoning(self) -> DVector:
        """
        Sums all the zone values in DVector into a single value.

        Returns a copy of Dvector. This function is equivalent to calling:
        self.remove_zoning(fn=np.sum)

        Returns
        -------
        summed_dvector:
            A copy of DVector, without any zoning.
        """
        return self.remove_zoning(fn=np.sum)

    def remove_zoning(self, fn: Callable) -> DVector:
        """
        Aggregates all the zone values in DVector into a single value using fn.

        Returns a copy of Dvector.

        Parameters
        ----------
        fn:
            The function to use when aggregating all zone values. fn must
            be able to take a np.array of values and return a single value
            in order for this to work.

        Returns
        -------
        summed_dvector:
            A copy of DVector, without any zoning.
        """
        # Validate fn
        if not callable(fn):
            raise ValueError(
                "fn is not callable. fn must be a function that "
                "takes an np.array of values and return a single value."
            )

        # Aggregate all the data
        keys = self._data.keys()
        values = [fn(x) for x in self._data.values()]

        return DVector(
            zoning_system=None,
            segmentation=self.segmentation,
            time_format=self.time_format,
            import_data=dict(zip(keys, values)),
            process_count=self.process_count,
            verbose=self.verbose,
        )

    def convert_time_format(self,
                            new_time_format: Union[str, TimeFormat],
                            ) -> DVector:
        # Validate the given value
        new_time_format = self._validate_time_format(new_time_format)

        # ## CHECK THIS IS A VALID CONVERSION ## #
        if self._time_format is None:
            raise ValueError(
                "Cannot convert the time format of a DVector that does not "
                "have a time format to begin with."
            )

        if new_time_format is None:
            raise ValueError(
                "Cannot convert the time format of a DVector to None. If "
                "a DVector has a time_format, it cannot be converted away."
            )

        if not self.segmentation.has_time_period_segments():
            segment_name = self.segmentation._time_period_segment_name
            raise ValueError(
                "Cannot convert the time_format of a DVector without a %s"
                "segment, as the conversion is different per segment. Please "
                "add a %s segment in and then convert."
                % (segment_name, segment_name)
            )

        # ## CONVERT THE TIME FORMAT ## #
        # If current time_format is the same as new, do nothing
        if self._time_format == new_time_format:
            return self.copy()

        # Get the data we need to convert
        conversion_factors = self._time_format.get_conversion_factors(new_time_format)
        tp_groups = self.segmentation.get_time_period_groups()

        # Check we have conversion factors for each needed time period
        segmentation_tps = set(tp_groups.keys())
        conversion_tps = set(conversion_factors.keys())
        missing_tps = segmentation_tps - conversion_tps
        if len(missing_tps) > 0:
            raise nd.SegmentationError(
                "This DVector is using a segmentation with time_periods in "
                "that we don't know how to convert. We only have conversion "
                "factors for the following time periods: %s.\n"
                "Missing conversion factors for the following time periods: %s"
                % (conversion_tps, missing_tps)
            )

        # Convert each time period of segments
        dvec_data = dict.fromkeys(self._data.keys())
        for tp, segments in tp_groups.items():
            for seg in segments:
                dvec_data[seg] = self._data[seg] * conversion_factors[tp]

        return DVector(
            zoning_system=self.zoning_system,
            segmentation=self.segmentation,
            time_format=new_time_format,
            import_data=dvec_data,
            process_count=self.process_count,
            verbose=self.verbose,
        )

    def write_sector_reports(self,
                             segment_totals_path: nd.PathLike,
                             ca_sector_path: nd.PathLike,
                             ie_sector_path: nd.PathLike,
                             ) -> None:
        """
        Writes segment, CA sector, and IE sector reports to disk

        Parameters
        ----------
        segment_totals_path:
            Path to write the segment totals report to

        ca_sector_path:
            Path to write the CA sector report to

        ie_sector_path:
            Path to write the IE sector report to

        Returns
        -------
        None
        """
        # Segment totals report
        df = self.sum_zoning().to_df()
        df.to_csv(segment_totals_path, index=False)

        # Segment by CA Sector total reports
        tfn_ca_sectors = nd.get_zoning_system('ca_sector_2020')
        df = self.translate_zoning(tfn_ca_sectors)
        df.to_df().to_csv(ca_sector_path, index=False)

        # Segment by IE Sector total reports
        ie_sectors = nd.get_zoning_system('ie_sector')
        df = self.translate_zoning(ie_sectors).to_df()
        df.to_csv(ie_sector_path, index=False)


class DVectorError(nd.NormitsDemandError):
    """
    Exception for all errors that occur around DVector management
    """
    def __init__(self, message=None):
        self.message = message
        super().__init__(self.message)


# ## FUNCTIONS ## #
