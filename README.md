# **XML Wizard**

## This project is currently in its initial stages of development and prototyping

This repository contains python code for converting XML to JSON, Parquet and Apache Arrow.

# Key Concept

Instead of parsing and validating elements one at a time, the XML Wizard will load XML into data vectors.\
Once in vector format, operations like string to numeric data casting and validation can be performed in bulk.

Using the XML below as an example:

We have a nested structure with CompanyDirectory > Department > Employees and Employee.

``` xml
<?xml version="1.0" encoding="UTF-8"?>
<CompanyDirectory>
    <!-- Level 1: Root -->
    
    <Department>
        <!-- Level 2: Parent Record -->
        <DeptName>Engineering</DeptName>
        <Budget>500000</Budget>
        
        <Employees>
            <!-- Level 3: Child Records (Multiple) -->
            <Employee>
                <EmpID>E001</EmpID>
                <Name>John Smith</Name>
                <Role>Developer</Role>
            </Employee>
            
            <Employee>
                <EmpID>E002</EmpID>
                <Name>Jane Doe</Name>
                <Role>Designer</Role>
            </Employee>
        </Employees>
        
    </Department>
    
    <Department>
        <!-- Level 2: Parent Record -->
        <DeptName>Sales</DeptName>
        <Budget>300000</Budget>
        
        <Employees>
            <!-- Level 3: Child Records (Multiple) -->
            <Employee>
                <EmpID>E003</EmpID>
                <Name>Bob Brown</Name>
                <Role>Manager</Role>
            </Employee>
        </Employees>
        
    </Department>

</CompanyDirectory>
```

The Employee data is saved using three vectors: EmpId, Name and Role.

| EmpID | Name | Role |
|-------|------|------|
| E001 | John Smith | Developer |
| E002 | Jane Doe | Designer |
| E003 | Bob Brown | Manager |

These employees belong to Departments which are saved in two vectors: DeptName and Budget.
An offset vector is added to track which employees belong to which department.

| DeptName | Budget | employee offsets |
|----------|--------|--------|
| Engineering | 500000 | 1 to 2 |
| Sales | 300000 | 3 to 3 |

This structure allows us to store all our data in single columns even if the xml data is deeply nested.

These vectors are populated as strings when the XML is parsed.

After parsing is completed we can run a column cast on Budget to convert it from a string to an integer.

When applying a XSD restriction check like Budget > 0, we only need to run that check once on the entire budget column.

# Key Features

Converts XML to valid JSON, Parquet or Apache Arrow objects.\
Requires only two files to get started. Your XML file and the XSD schema file for that XML file.\
Multiprocessing enabled to parse XML files concurrently if the XML files are in the same format. Call with -m # option.\
Uses Python's iterparse event based methods which enables parsing very large files with low memory requirements.\
This is very similar to Java's SAX parser.\
Files are processed in order with the largest files first to optimize overall parsing time

# How to run?
```shell
python xml_wiz.py
```

# Parameters
```shell
usage: xml_wiz.py [-h] -x XSD_FILE [--max_recursion MAX_RECURSION] [-p XPATH]
                  [--rows_per_batch ROWS_PER_BATCH] [-m MULTI] [-o OUTPUT_FORMAT]
                  [-t OUTPUT_PATH] [-z] [--no_overwrite] [--delete_xml]
                  [--flatten [FLATTEN]] [-l LOG_LEVEL] [--log_file LOG_FILE]
                  ...
XML Wizard positional arguments: xml_files | xml files to convert
```

| Option | Description |
|--------|-------------|
| -h, --help | show this help message and exit |
| -x XSD_FILE, --xsd_file XSD_FILE | xsd file location. |
| --max_recursion MAX_RECURSION | max recursions for self referencing elements. |
| -p XML_PATH, --xml_path XML_PATH | xml path to parse. |
| --rows_per_batch ROWS_PER_BATCH |  number of rows to write per batch when using xpath. |
| -m MULTI, --multi MULTI | number of parsers. default is 1. |
| -o OUTPUT_FORMAT, --output_format OUTPUT_FORMAT | output format `json`, `jsonl` or `parquet`. default is jsonl. |
| -t OUTPUT_PATH, --output_path OUTPUT_PATH | output directory. |
| -z, --gzipfile | gzip output json file. |
| --no_overwrite | do not overwrite output file if it exists already. |
| --delete_xml | delete xml file after conversion. |
| --flatten [FLATTEN] | Flatten results. (optional `attributes` or `elements`). |
| -l LOG_LEVEL, --log_level LOG_LEVEL | logging level. INFO, DEBUG, etc. |
| --log_file LOG_FILE | log file location. |
| xml_files | list of xml files to convert. can include wildcards. |


# Convert a small XML file to a JSONL file
```shell
python xml_wiz.py -x PurchaseOrder.xsd PurchaseOrder.xml
INFO - 2026-06-21 18:28:12 - Parsing XML Files..
INFO - 2026-06-21 18:28:12 - Processing 1 files
INFO - 2026-06-21 18:28:12 - Generating schema from PurchaseOrder.xsd
INFO - 2026-06-21 18:28:12 - Parsing PurchaseOrder.xml
INFO - 2026-06-21 18:28:12 - Writing to file PurchaseOrder.jsonl
INFO - 2026-06-21 18:28:12 - Completed PurchaseOrder.xml
```

# Convert a small XML file to a Parquet file
```shell
python xml_wiz.py -o parquet -x PurchaseOrder.xsd PurchaseOrder.xml
INFO - 2026-06-21 18:30:03 - Parsing XML Files..
INFO - 2026-06-21 18:30:03 - Processing 1 files
INFO - 2026-06-21 18:28:12 - Generating schema from PurchaseOrder.xsd
INFO - 2026-06-21 18:30:03 - Parsing PurchaseOrder.xml
INFO - 2026-06-21 18:30:03 - Writing to file PurchaseOrder.parquet
INFO - 2026-06-21 18:30:03 - Completed PurchaseOrder.xml
```

Original XML
```xml
<?xml version="1.0"?>
<purchaseOrder orderDate="1999-10-20">
    <shipTo country="US">
        <name>Alice Smith</name>
        <street>123 Maple Street</street>
        <city>Mill Valley</city>
        <state>CA</state>
        <zip>90952</zip>
    </shipTo>
    <billTo country="US">
        <name>Robert Smith</name>
        <street>8 Oak Avenue</street>
        <city>Old Town</city>
        <state>PA</state>
        <zip>95819</zip>
    </billTo>
    <comment>Hurry, my lawn is going wild!</comment>
    <items>
        <item partNum="872-AA">
            <productName>Lawnmower</productName>
            <quantity>1</quantity>
            <USPrice>148.95</USPrice>
            <comment>Confirm this is electric</comment>
        </item>
        <item partNum="926-AA">
            <productName>Baby Monitor</productName>
            <quantity>1</quantity>
            <USPrice>39.98</USPrice>
            <shipDate>1999-05-21</shipDate>
        </item>
    </items>
</purchaseOrder>
```

JSON output
(zip looks funny, but blame Microsoft which says zip is a xs:decimal in their sample XSD file)  
https://learn.microsoft.com/en-us/visualstudio/xml-tools/sample-xsd-file-simple-schema
```json
{   
   "purchaseOrderorderDate":"1999-10-20",
   "shipTo":{   
      "shipTocountry":"US",
      "name":"Alice Smith",
      "street":"123 Maple Street",
      "city":"Mill Valley",
      "state":"CA",
      "zip":90952.0
   },
   "billTo":{   
      "billTocountry":"US",
      "name":"Robert Smith",
      "street":"8 Oak Avenue",
      "city":"Old Town",
      "state":"PA",
      "zip":95819.0
   },
   "comment":"Hurry, my lawn is going wild!",
   "items":{   
      "item":[   
         {   
            "itempartNum":"872-AA",
            "productName":"Lawnmower",
            "quantity":1,
            "USPrice":148.95,
            "comment":"Confirm this is electric"
         },
         {   
            "itempartNum":"926-AA",
            "productName":"Baby Monitor",
            "quantity":1,
            "USPrice":39.98,
            "shipDate":"1999-05-21"
         }
      ]
   }
}
```

# Convert an entire directory of XML files to JSONL
Also zip output files, parse 3 files concurrently, only extract /PurchaseOrder/items/item elements and incrementally
process one XML path at a time to save memory instead of trying to read the entire XML file into memory.
```shell
cp PurchaseOrder.xml 1.xml
cp PurchaseOrder.xml 2.xml

python xml_wiz.py -m 2 -z -p /purchaseOrder/items/item -x PurchaseOrder.xsd *.xml

INFO - 2026-06-21 18:33:28 - Parsing XML Files..
INFO - 2026-06-21 18:33:29 - Processing 3 files
INFO - 2026-06-21 18:33:29 - Parsing files in the following order:
INFO - 2026-06-21 18:33:29 - ['PurchaseOrder.xml', '1.xml', '2.xml']

DEBUG - 2018-03-20 16:33:50 - Generating schema from PurchaseOrder.xsd
DEBUG - 2018-03-20 16:33:50 - Generating schema from PurchaseOrder.xsd
DEBUG - 2018-03-20 16:33:50 - Parsing PurchaseOrder.xml
DEBUG - 2018-03-20 16:33:50 - Writing to file PurchaseOrder.jsonl.gz
DEBUG - 2018-03-20 16:33:50 - Parsing 1.xml
DEBUG - 2018-03-20 16:33:50 - Parsing 2.xml
DEBUG - 2018-03-20 16:33:50 - Writing to file 1.jsonl.gz
DEBUG - 2018-03-20 16:33:50 - Writing to file 2.jsonl.gz
DEBUG - 2018-03-20 16:33:51 - Parsing item from 1.xml
DEBUG - 2018-03-20 16:33:51 - Parsing item from 2.xml
DEBUG - 2018-03-20 16:33:51 - Parsing item from PurchaseOrder.xml
DEBUG - 2018-03-20 16:33:51 - Completed 2.xml
DEBUG - 2018-03-20 16:33:51 - Generating schema from PurchaseOrder.xsd
DEBUG - 2018-03-20 16:33:51 - Completed PurchaseOrder.xml
DEBUG - 2018-03-20 16:33:51 - Completed 1.xml
DEBUG - 2018-03-20 16:33:51 - Generating schema from PurchaseOrder.xsd
DEBUG - 2018-03-20 16:33:51 - Parsing 4.xml
DEBUG - 2018-03-20 16:33:51 - Writing to file 4.jsonl.gz
DEBUG - 2018-03-20 16:33:51 - Parsing 3.xml
DEBUG - 2018-03-20 16:33:51 - Writing to file 3.jsonl.gz
DEBUG - 2018-03-20 16:33:51 - Parsing item from 3.xml
DEBUG - 2018-03-20 16:33:51 - Parsing item from 4.xml
DEBUG - 2018-03-20 16:33:51 - Completed 3.xml
DEBUG - 2018-03-20 16:33:51 - Completed 4.xml
```
JSON output
```json
ls -l *.gz
-rw-r--r-- 1 user users 191 Mar 20 16:26 1.jsonl.gz
-rw-r--r-- 1 user users 191 Mar 20 16:26 2.jsonl.gz
-rw-r--r-- 1 user users 191 Mar 20 16:26 3.jsonl.gz
-rw-r--r-- 1 user users 191 Mar 20 16:26 4.jsonl.gz
-rw-r--r-- 1 user users 203 Mar 20 16:26 PurchaseOrder.jsonl.gz

zcat *.jsonl.gz

{"itempartNum": "872-AA", "productName": "Lawnmower", "quantity": 1, "USPrice": 148.95, "comment": "Confirm this is electric"}
{"itempartNum": "926-AA", "productName": "Baby Monitor", "quantity": 1, "USPrice": 39.98, "shipDate": "1999-05-21"}

{"itempartNum": "872-AA", "productName": "Lawnmower", "quantity": 1, "USPrice": 148.95, "comment": "Confirm this is electric"}
{"itempartNum": "926-AA", "productName": "Baby Monitor", "quantity": 1, "USPrice": 39.98, "shipDate": "1999-05-21"}

{"itempartNum": "872-AA", "productName": "Lawnmower", "quantity": 1, "USPrice": 148.95, "comment": "Confirm this is electric"}
{"itempartNum": "926-AA", "productName": "Baby Monitor", "quantity": 1, "USPrice": 39.98, "shipDate": "1999-05-21"}

{"itempartNum": "872-AA", "productName": "Lawnmower", "quantity": 1, "USPrice": 148.95, "comment": "Confirm this is electric"}
{"itempartNum": "926-AA", "productName": "Baby Monitor", "quantity": 1, "USPrice": 39.98, "shipDate": "1999-05-21"}

{"itempartNum": "872-AA", "productName": "Lawnmower", "quantity": 1, "USPrice": 148.95, "comment": "Confirm this is electric"}
{"itempartNum": "926-AA", "productName": "Baby Monitor", "quantity": 1, "USPrice": 39.98, "shipDate": "1999-05-21"}
```
