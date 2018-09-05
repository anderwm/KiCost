# MIT license
#
# Copyright (C) 2018 by XESS Corporation / Hildo Guillardi Junior
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

# Inserted by Pasteurize tool.
from __future__ import print_function, unicode_literals, division, absolute_import
from builtins import zip, range, int, str
from future import standard_library
standard_library.install_aliases()
import future

import sys, os
import copy
import re
import pprint
import tqdm
from time import time
#from multiprocessing.pool import ThreadPool
import json
import requests

# Stops UnicodeDecodeError exceptions.
try:
    reload(sys)
    sys.setdefaultencoding('utf8')
except NameError:
    pass  # Happens if reload is attempted in Python 3.

# ghost library allows scraping pages that have JavaScript challenge pages that
# screen-out robots. Digi-Key stopped doing this, so it's not needed at the moment.
# Also requires installation of Qt4.8 (not 5!) and pyside.
#from ghost import Ghost

__all__ = ['kicost','output_filename']  # Only export this routine for use by the outside world.

from .global_vars import *

# Import information about various distributors.
from .distributors import *
from .distributors.global_vars import distributor_dict

# Import information for various EDA tools.
from .eda_tools import eda_modules
from .eda_tools.eda_tools import subpartqty_split, group_parts

from .spreadsheet import * # Creation of the final XLSX spreadsheet.

def kicost(in_file, eda_tool_name, out_filename,
        user_fields, ignore_fields, group_fields, variant,
        dist_list=list(distributor_dict.keys()),
        num_processes=4, scrape_retries=5, throttling_delay=5.0,
        collapse_refs=True,
        local_currency='USD'):
    ''' @brief Run KiCost.
    
    Take a schematic input file and create an output file with a cost spreadsheet in xlsx format.
    
    @param in_file `list(str())` List of the names of the input BOM files.
    @param eda_tool_name `list(str())` of the EDA modules to be used to open the `in_file`list.
    @param out_filename `str()` XLSX output file name.
    @param user_fields `list()` of the user fields to be included on the spreadsheet global part.
    @param ignore_fields `list()` of the fields to be ignored on the read EDA modules.
    @param group_fields `list()` of the fields to be grouped/merged on the function group parts that
    are not grouped by default.
    @param variant `list(str())` of regular expression to the BOM variant of each file in `in_file`.
    @param dist_list `list(str())` to be scraped, if empty will be scraped with all distributors
    modules. If `None`, no web/local distributors will be scraped.
    @param num_processes `int()` Number of parallel processes used for web scraping part data. Use
    1 for serial mode.
    @param scrape_retries `int()` Number of attempts to retrieve part data from a website..
    @param throttling_delay `float()` Minimum delay (in seconds) between successive accesses to a
    distributor's website.
    @param collapse_refs `bool()` Collapse or not the designator references in the spreadsheet.
    Default `True`.
    @param local_currency `str()` Local/country in ISO3166:2 and currency in ISO4217. Default 'USD'.
    '''

    logger.log(DEBUG_OVERVIEW, 'Exchange rate: 1 EUR = %.2f USD' % currency.convert(1, 'EUR', 'USD'))

    # Only keep distributors in the included list and not in the excluded list.
    if dist_list!=None:
        if not dist_list:
            dist_list = list(distributor_dict.keys())
        if not 'local_template' in dist_list:
            dist_list += ['local_template'] # Needed later for creating non-web distributors.
        for d in list(distributor_dict.keys()):
            if not d in dist_list:
                distributor_dict.pop(d, None)
    else:
        for d in list(distributor_dict.keys()):
            distributor_dict.pop(d, None)

    # Deal with some code exception (only one EDA tool or variant
    # informed in the multiple BOM files input).
    if not isinstance(in_file,list):
        in_file = [in_file]
    if not isinstance(variant,list):
        variant = [variant] * len(in_file)
    elif len(variant) != len(in_file):
        variant = [variant[0]] * len(in_file) #Assume the first as default.
    if not isinstance(eda_tool_name,list):
        eda_tool_name = [eda_tool_name] * len(in_file)
    elif len(eda_tool_name) != len(in_file):
        eda_tool_name = [eda_tool_name[0]] * len(in_file) #Assume the first as default.

    # Get groups of identical parts.
    parts = dict()
    prj_info = list()
    for i_prj in range(len(in_file)):
        eda_tool_module = eda_modules[eda_tool_name[i_prj]]
        p, info = eda_tool_module.get_part_groups(in_file[i_prj], ignore_fields, variant[i_prj])
        p = subpartqty_split(p)
        # In the case of multiple BOM files, add the project prefix identifier
        # to each reference/designator. Use the field 'manf#_qty' to control
        # each quantity goes to each project creating a `list()` with length
        # of number of BOM files. This vector will be used in the `group_parts()`
        # to create groups with elements of same 'manf#' that came for different
        # projects.
        if len(in_file)>1:
            logger.log(DEBUG_OVERVIEW, 'Multi BOMs detected, attaching project identification to references...')
            qty_base = ['0'] * len(in_file) # Base zero quantity vector.
            for p_ref in list(p.keys()):
                try:
                    qty_base[i_prj] = p[p_ref]['manf#_qty']
                except:
                    qty_base[i_prj] = '1'
                p[p_ref]['manf#_qty'] = qty_base.copy()
                p[ 'prj' + str(i_prj) + SEPRTR + p_ref] = p.pop(p_ref)
        parts.update( p.copy() )
        prj_info.append( info.copy() )

    # Group part out of the module to be possible to merge different
    # project lists, ignore some field to merge given in the `group_fields`.
    FIELDS_SPREADSHEET = ['refs', 'value', 'desc', 'footprint', 'manf', 'manf#']
    FIELDS_MANFCAT = ([d + '#' for d in distributor_dict] + ['manf#'])
    FIELDS_MANFQTY = ([d + '#_qty' for d in distributor_dict] + ['manf#_qty'])
    FIELDS_IGNORE = FIELDS_SPREADSHEET + FIELDS_MANFCAT + FIELDS_MANFQTY + user_fields + ['pricing']
    for ref, fields in list(parts.items()):
        for f in fields:
            # Merge all extra fields that read on the files that will
            # not be displayed (Needed to check `user_fields`).
            if f not in FIELDS_IGNORE and SEPRTR not in f and not f in group_fields: # Not include repetitive filed names or fields with the separator `:` defined on `SEPRTR`.
                group_fields += [f]
    # Some fields to be merged on specific EDA are enrolled bellow.
    if 'kicad' in eda_tool_name:
        group_fields += ['libpart'] # This field may be a mess on multiple sheet designs.
    if len(set(eda_tool_name))>2:
        # If more than one EDA software was used, ignore the 'footprint'
        # field, because they could have different libraries names.
        group_fields += ['footprint']
    group_fields += ['desc', 'var'] # Always ignore 'desc' ('description')
                                    # and 'var' ('variant') fields, merging
                                    # the components in groups.
    group_fields = set(group_fields)
    parts = group_parts(parts, group_fields)

    # If do not have the manufacture code 'manf#' and just distributors codes,
    # check if is asked to scrape a distributor that do not have any code in the
    # parts so, exclude this distributors for the scrap list. This decrease the
    # warning messages given during the process.
    all_fields = []
    for p in parts:
        all_fields += list(p.fields.keys())
    all_fields = set(all_fields)
    if not 'manf#' in all_fields:
        dist_not_rmv = [d for d in distributor_dict.keys() if d+'#' in all_fields]
        dist_not_rmv += ['local_template'] # Needed later for creating non-web distributors.
        #distributor_scrap = {d:distributor_dict[d] for d in dist_not_rmv}
        distributors = distributor_dict.copy().keys()
        for d in distributors:
            if not d in dist_not_rmv:
                logger.warning("No 'manf#' and '%s#' field in any part: distributor '%s' will be not scraped.", d, distributor_dict[d]['label'])
                distributor_dict.pop(d, None)

    if logger.isEnabledFor(DEBUG_DETAILED):
        pprint.pprint(distributor_dict)

    ##############################################################################
    # Handle locally-sourced parts.
    ##############################################################################

    # This loops through all the parts and finds any that are sourced from 
    # local distributors that are not normally searched and places them into 
    # the distributor disctionary.
    for part in parts:
        # Find the various distributors for this part by
        # looking for leading fields terminated by SEPRTR.
        for key in part.fields:
            try:
                dist = key[:key.index(SEPRTR)]
            except ValueError:
                continue

            # If the distributor is not in the list of web-scrapable distributors,
            # then it's a local distributor. Copy the local distributor template
            # and add it to the table of distributors.
            if dist not in distributor_dict:
                distributor_dict[dist] = copy.copy(distributor_dict['local_template'])
                distributor_dict[dist]['label'] = dist  # Set dist name for spreadsheet header.

    # Set part info to default values for all the distributors.
    for part in parts:
        part.part_num = {dist:'' for dist in distributor_dict}
        part.url = {dist:'' for dist in distributor_dict}
        part.price_tiers = {dist:{} for dist in distributor_dict}
        part.qty_avail = {dist:None for dist in distributor_dict}
        part.qty_increment = {dist:None for dist in distributor_dict}
        part.info_dist = {dist:{} for dist in distributor_dict}

    # Loop through the parts looking for those sourced by local distributors
    # that won't be found online. Place any user-added info for these parts
    # (such as pricing) into the part dictionary.
    for p in parts:
        # Find the manufacturer's part number if it exists.
        pn = p.fields.get('manf#') # Returns None if no manf# field.

       # Now look for catalog number, price list and webpage link for this part.
        for dist in distributor_dict:
            cat_num = p.fields.get(dist+':cat#')
            pricing = p.fields.get(dist+':pricing')
            link = p.fields.get(dist+':link')
            if cat_num is None and pricing is None and link is None:
                continue

            def make_random_catalog_number(p):
                hash_fields = {k: p.fields[k] for k in p.fields}
                hash_fields['dist'] = dist
                return '#{0:08X}'.format(abs(hash(tuple(sorted(hash_fields.items())))))

            cat_num = cat_num or pn or make_random_catalog_number(p)
            p.fields[dist+':cat#'] = cat_num # Store generated cat#.
            p.part_num[dist] = cat_num

            link = ''
            try:
                url_parts = list(urlsplit(link))
                if url_parts[0] == '':
                    url_parts[0] = u'http'
                link = urlunsplit(url_parts)
            except Exception:
                # This happens when no part URL is found.
                logger.log(DEBUG_OBSESSIVE, 'No local part URL found!')
            p.url[dist] = link
                
            price_tiers = {}
            try:
                pricing = re.sub('[^0-9.;:]', '', pricing) # Keep only digits, decimals, delimiters.
                for qty_price in pricing.split(';'):
                    qty, price = qty_price.split(SEPRTR)
                    price_tiers[int(qty)] = float(price)
            except AttributeError:
                # This happens when no pricing info is found.
                logger.log(DEBUG_OBSESSIVE, 'No local pricing information found!')
            p.price_tiers[dist] = price_tiers

    # Remove the local distributor template so it won't be processed later on.
    # It has served its purpose.
    try:
        del distributor_dict['local_template']
    except:
        pass

    ##############################################################################
    # Done handling locally-sourced parts.
    ##############################################################################

    num_processes = min(num_processes, len(distributor_dict))
    logger.log(DEBUG_OBSESSIVE, "Initialising scraper with %d threads" % num_processes)
    logger.log(DEBUG_OBSESSIVE, "throttling_delay=%d" % throttling_delay)

    # Get the distributor product page for each part and scrape the part data.
    if dist_list:

        scraping_progress = tqdm.tqdm(desc='Progress', \
            total=len(parts), unit='part', miniters=1)

        # Change the logging print channel to `tqdm` to keep the process bar to the end of terminal.
        class TqdmLoggingHandler(logging.Handler):
            '''Overload the class to write the logging through the `tqdm`.'''
            def __init__(self, level = logging.NOTSET):
                super(self.__class__, self).__init__(level)
            def emit(self, record):
                try:
                    msg = self.format(record)
                    tqdm.tqdm.write(msg)
                    self.flush()
                except (KeyboardInterrupt, SystemExit):
                    raise
                except:
                    self.handleError(record)
                pass
        # Get handles to default sys.stdout logging handler and the
        # new "tqdm" logging handler.
        logDefaultHandler = logger.handlers[0]
        logTqdmHandler = TqdmLoggingHandler()

        # Replace default handler with "tqdm" handler.
        logger.addHandler(logTqdmHandler)
        logger.removeHandler(logDefaultHandler)

        # # Create thread pool to init multiple distributors simultaneously.
        # pool = ThreadPool(num_processes)

        # # Package part data for passing to each process.
        # arg_sets = [(d, distributor_dict[d]['scrape']) for d in distributor_dict]

        # def mt_init_dist(d, scrape):
            # instance = None
            # try:
                # logger.log(DEBUG_OVERVIEW, "Initialising %s" % d)
                # if scrape == 'local':
                    # ctor = globals()['dist_local']
                # else:
                    # ctor = globals()['dist_'+d]
                # instance = ctor(d, scrape_retries, throttling_delay)
            # except Exception as ex:
                # logger.log(DEBUG_OVERVIEW, "Initialising %s failed with %s, excluding this distributor..." \
                    # % (d, type(ex).__name__))
                # return (d, None)

            # if local_currency:
                # logger.log(DEBUG_OVERVIEW, '# Configuring the distributors locale and currency...')
                # instance.define_locale_currency(local_currency)
            # return (d, instance)

        # logger.log(DEBUG_OBSESSIVE, 'Starting {} threads to init distributors...'.format(num_processes))
        # results = [pool.apply_async(mt_init_dist, args) for args in arg_sets]

        # # Wait for all the processes to have results.
        # pool.close()
        # pool.join()

        # # Get the data from each process result structure.
        # for result in results:
            # d, instance = result.get()
            # # Distributor initialisation failed, remove it from distributor_dict.
            # if instance == None:
                # distributor_dict.pop(d, None)
            # # Distributor initialised successfully, add instance to distributor_dict.
            # else:
                # distributor_dict[d]['instance'] = instance

        ##########################################################################
        # Get part data from Octopart.
        ##########################################################################

        logger.log(DEBUG_OVERVIEW, '# Getting part data from Octopart...')

        # Translate from Octopart distributor names to the names used internally by kicost.
        dist_xlate = {'Digi-Key':'digikey', 'Mouser':'mouser', 'Newark':'newark', 'Farnell':'farnell', 'RS Components':'rs', 'TME':'tme', 'Arrow Electronics, Inc.':'arrow'}

        def get_part_info(query, parts):
            """Query Octopart for quantity/price info and place it into the parts list."""

            # Create query URL for Octopart.
            url = 'http://octopart.com/api/v3/parts/match'
            payload = {'queries':json.dumps(query), 'apikey':'96df69ba'}
            response = requests.get(url, params=payload)
            results = json.loads(response.text)['results']

            # Loop through the response to the query and enter part info into the parts list.
            for result in results:
                i = int(result['reference']) # Get the index into the part dictionary.

                # Loop through the offers from various distributors for this particular part.
                for item in result['items']:
                    for offer in item['offers']:

                        # Get the distributor who made the offer and add their 
                        # price/qty info to the parts list if its one of the accepted distributors.
                        dist = dist_xlate.get(offer['seller']['name'], '')
                        if dist in distributor_dict:
                            # Get pricing information from this distributor.
                            try:
                                price_tiers = {} # Empty dict in case of exception.
                                price_tiers = {qty:float(price) for qty, price in list(offer['prices'].values())[0]}
                                # Combine price lists for multiple offers from the same distributor
                                # to build a complete list of cut-tape and reeled components.
                                parts[i].price_tiers[dist].update(price_tiers)
                            except Exception:
                                pass  # Price list is probably missing so leave empty default dict in place.

                            # Compute the quantity increment between the lowest two prices.
                            # This will be used to distinguish the cut-tape from the reeled components.
                            try:
                                part_break_qtys = sorted(price_tiers.keys())
                                part_qty_increment = part_break_qtys[1] - part_break_qtys[0]
                            except Exception:
                                pass

                            # Use the qty increment to select the part SKU, web page, and available quantity.
                            # Do this if this is the first part offer from this dist.
                            if not parts[i].part_num[dist]:
                                parts[i].part_num[dist] = offer.get('sku', '')
                                parts[i].url[dist] = offer.get('product_url', '')
                                parts[i].qty_avail[dist] = offer.get('in_stock_quantity', None)
                                parts[i].qty_increment[dist] = part_qty_increment
                            # Otherwise, check qty increment and see if its the smallest for this part & dist.
                            elif part_qty_increment < parts[i].qty_increment[dist]:
                                    # This part looks more like a cut-tape version, so
                                    # update the SKU, web page, and available quantity.
                                    parts[i].part_num[dist] = offer.get('sku', '')
                                    parts[i].url[dist] = offer.get('product_url', '')
                                    parts[i].qty_avail[dist] = offer.get('in_stock_quantity', None)
                                    parts[i].qty_increment[dist] = part_qty_increment

                            # Don't bother with any extra info from the distributor.
                            parts[i].info_dist[dist] = {}

        # Break list of parts into smaller pieces and get price/quantities from Octopart.
        octopart_query = []
        for i, part in enumerate(parts):

            # Get manufacturer's part number for doing Octopart search.
            try:
                mpn = part.fields['manf#']
            except KeyError:
                continue # No manf. part number, so don't add this part to the query.

            # Add query for this part to the list of part queries.
            part_query = dict([('reference', i), ('mpn', mpn)])
            octopart_query.append(part_query)

            # Once there are enough (but not too many) part queries, make a query request to Octopart.
            if len(octopart_query) == 20:
                get_part_info(octopart_query, parts)
                scraping_progress.update(len(octopart_query))
                octopart_query = [] # Clear list of queries to get ready for next batch.

        # Query Octopart for the last batch of parts.
        if octopart_query:
            get_part_info(octopart_query, parts)
            scraping_progress.update(len(octopart_query))


        ##########################################################################
        # Done with Octopart.
        ##########################################################################

        # Return the print channel of the logging.
        logger.addHandler(logDefaultHandler)
        logger.removeHandler(logTqdmHandler)

        # Done with the scraping progress bar so delete it or else we get an 
        # error when the program terminates.
        del scraping_progress

    # Create the part pricing spreadsheet.
    create_spreadsheet(parts, prj_info, out_filename, collapse_refs,
                      user_fields, '-'.join(variant) if len(variant)>1 else variant[0])

    # Print component groups for debugging purposes.
    if logger.isEnabledFor(DEBUG_DETAILED):
        for part in parts:
            for f in dir(part):
                if f.startswith('__'):
                    continue
                elif f.startswith('html_trees'):
                    continue
                else:
                    print('{} = '.format(f), end=' ')
                    try:
                        pprint.pprint(part.__dict__[f])
                    except TypeError:
                        # Python 2.7 pprint has some problem ordering None and strings.
                        print(part.__dict__[f])
                    except KeyError:
                        pass
            print()




FILE_OUTPUT_MAX_NAME = 10 # Maximum length of the name of the spreadsheet output
                          # generate, this is used in the multifiles to limit the
                          # automatic name generation.
FILE_OUTPUT_MIN_INPUT = 5 # Minimum length of characters to use of the input files
                          # to create the name of the spreadsheet output file. This
                          # is used in the multifile BoM and have prioritize in the
                          # `FILE_OUTPUT_MAX_NAME` definition.
FILE_OUTPUT_INPUT_SEP = '-' # Separator in the name of the output spreadsheet file
                            # when used multiple input file to generate automatically
                            # the name.
# Here because is used at `__main__.py` and `kicost_gui.py`.
def output_filename(files_input):
    ''' @brief Compose a name with the multiple BOM input file names.

    Compose a name with the multiple BOM input file names, limiting to,
    at least, the first `FILE_OUTPUT_MIN_INPUT` characters of each name
    (avoid huge names by `FILE_OUTPUT_MAX_NAME`definition). Join the names
    of the input files by `FILE_OUTPUT_INPUT_SEP` definition.
    The output folder is the folder of the first file.
    @param files_input `list()`of the input file names.
    @return `str()` file name for the spreadsheet.
    '''

    if len(files_input)==1:
        # Use the folder of the project.
        return os.path.splitext(files_input[0])[0] + '.xlsx'
    else:
        # If more the one file selected, check if they are in
        # the same folder, if don't, output in the folder where
        # `kicost` was called.
        dir_output = os.path.dirname(files_input[0]) + os.path.sep
        for dir_idx in range(len(files_input)):
            if os.path.dirname(files_input[dir_idx])!=dir_output:
                dir_output = os.getcwd()

    file_output = dir_output + FILE_OUTPUT_INPUT_SEP.join( [ os.path.splitext(os.path.basename(input_name))[0][:max(int(FILE_OUTPUT_MAX_NAME/len(files_input)),FILE_OUTPUT_MIN_INPUT-len(FILE_OUTPUT_INPUT_SEP))] for input_name in files_input ] ) + '.xlsx'
    return file_output
