
import consts
import os, sys
import datetime
import logging
import csv, json
from models import PricingRecord, PricingResult
from errors import NoDataFoundError

log = logging.getLogger()

__location__ = os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))
site_pkgs = os.path.abspath(os.path.join(__location__, os.pardir, os.pardir,"lib", "python2.7", "site-packages" ))
sys.path.append(site_pkgs)
#print "site_pkgs: [{}]".format(site_pkgs)

import tinydb


def get_data_directory(service):
  result = os.path.split(__location__)[0] + '/data/' + service + '/'
  return result



def getBillableBand(priceDimensions, usageAmount):
  billableBand = 0
  beginRange = int(priceDimensions['beginRange'])
  endRange = priceDimensions['endRange']
  pricePerUnit = priceDimensions['pricePerUnit']['USD']
  if endRange == consts.INFINITY:
    if beginRange < usageAmount:
      billableBand = usageAmount - beginRange
  else:
    endRange = int(endRange)
    if endRange >= usageAmount and beginRange < usageAmount:
      billableBand = usageAmount - beginRange
    if endRange < usageAmount: 
      billableBand = endRange - beginRange
  return billableBand


def getBillableBandCsv(row, usageAmount):
    billableBand = 0
    pricePerUnit = 0
    amt = 0

    if not row['StartingRange']:beginRange = 0
    else: beginRange = int(row['StartingRange'])
    if not row['EndingRange']:endRange = consts.INFINITY
    else: endRange = row['EndingRange']

    pricePerUnit = float(row['PricePerUnit'])
    if endRange == consts.INFINITY:
      if beginRange < usageAmount:
        billableBand = usageAmount - beginRange
    else:
      endRange = int(endRange)
      if endRange >= usageAmount and beginRange < usageAmount:
        billableBand = usageAmount - beginRange
      if endRange < usageAmount:
        billableBand = endRange - beginRange

    if billableBand > 0: amt = pricePerUnit * billableBand

    return billableBand, pricePerUnit, amt



#Creates a table with all the SKUs that are part of the total price
def buildSkuTable(evaluated_sku_desc):
  result = {}
  sorted_descriptions = sorted(evaluated_sku_desc)
  result_table_header = "Price | Description | Price Per Unit | Usage | Rate Code"
  result_records = ""
  total = 0
  for s in sorted_descriptions:
    result_records = result_records + "$" + str(s[0]) + "|" + str(s[1]) + "|" + str(s[2]) + "|" + str(s[3]) + "|" + s[4]+"\n"
    total = total + s[0]
  
  result['header']=result_table_header
  result['records']=result_records
  result['total']=total
  return result



"""
Calculates the keys that will be used to partition big index files into smaller pieces.
If no term is specified, the function will consider On-Demand and Reserved
"""
def get_partition_keys(region, term, **extraArgs):
    result = []
    if region:
      regions = [consts.REGION_MAP[region]]
    else:
      regions = consts.REGION_MAP.values()

    if term: terms = [consts.TERM_TYPE_MAP[term]]
    else: terms = consts.TERM_TYPE_MAP.values()

    productFamilies = consts.SUPPORTED_PRODUCT_FAMILIES

    #EC2 Reserved
    offeringClasses = extraArgs.get('offeringClasses',consts.EC2_OFFERING_CLASS_MAP.values())
    tenancies = extraArgs.get('tenancies',consts.EC2_TENANCY_MAP.values())
    purchaseOptions = extraArgs.get('purchaseOptions',consts.EC2_PURCHASE_OPTION_MAP.values())

    indexDict = {}
    for r in regions:
        for t in terms:
            for pf in productFamilies:
                #Reserved EC2 instances have more dimensions for index creation
                if t == consts.TERM_TYPE_RESERVED:
                    for oc in offeringClasses:
                        for ten in tenancies:
                            for po in purchaseOptions:
                              result.append(create_file_key((r,t,pf,oc,ten, po)))
                else:
                    result.append(create_file_key((r,t,pf)))

    #print ("get_partition_keys - number of partition keys: [{}]".format(len(result)))
    return result


#Creates a file key that identifies a data partition
def create_file_key(indexDimensions):
    result = ""
    for d in indexDimensions: result += d
    return result.replace(' ','')



def loadDBs(service, indexFiles):

    dBs = {}
    datadir = get_data_directory(service)
    indexMetadata = getIndexMetadata(service)

    #Files in Lambda can only be created in the /tmp filesystem - If it doesn't exist, create it.
    lambdaFileSystem = '/tmp/'+service+'/data'
    if not os.path.exists(lambdaFileSystem):
      os.makedirs(lambdaFileSystem)

    for i in indexFiles:
      db = tinydb.TinyDB(lambdaFileSystem+'/'+i+'.json')
      #TODO: remove circular dependency from utils, so I can use the method get_index_file_name
      #TODO: initial tests show that is faster (by a few milliseconds) to populate the file from scratch). See if I should load from scratch all the time
      #TODO:Create a file that is an index of those files that have been generated, so the code knows which files to look for and avoid creating unnecesary empty .json files
      if len(db) == 0:
        try:
          with open(datadir+i+'.csv', 'rb') as csvfile:
              pricelist = csv.DictReader(csvfile, delimiter=',', quotechar='"')
              db.insert_multiple(pricelist)
        except IOError:
          pass
      dBs[i]=db

    return dBs, indexMetadata



def getIndexMetadata(service):
  result = {}
  #datadir = get_data_directory(service)
  with open(get_data_directory(service)+"index_metadata.json") as index_metadata:
    result = json.load(index_metadata)

  return result


def calculate_price(service, db, query, usageAmount, pricingRecords, cost):
  resultSet = db.search(query)
  if not resultSet: raise NoDataFoundError("Could not find data for service:[{}] - query:[{}]".format(service, query))
  #print("resultSet:[{}]".format(json.dumps(resultSet,indent=4)))
  for r in resultSet:
    billableUsage, pricePerUnit, amt = getBillableBandCsv(r, usageAmount)
    cost = cost + amt
    if billableUsage:
      pricing_record = PricingRecord(service,round(amt,4),r['PriceDescription'],pricePerUnit,billableUsage,r['RateCode'])
      pricingRecords.append(vars(pricing_record))

  return pricingRecords, cost



class Timestamp():

  def __init__(self):
    self.eventdict = {}

  def start(self,event):
    self.eventdict[event] = {}
    self.eventdict[event]['start'] = datetime.datetime.now()

  def finish(self, event):
    #elapsed = datetime.timedelta(self.eventdict[event]['start']) * 1000 #return milliseconds
    elapsed = datetime.datetime.now() - self.eventdict[event]['start']
    self.eventdict[event]['elapsed'] = elapsed
    return elapsed

  def elapsed(self,event):
    return self.eventdict[event]['elapsed']

