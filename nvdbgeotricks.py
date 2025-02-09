"""
En samling hjelpefunksjoner som bruker nvdbapiv3-funksjonene til å gjøre nyttige ting, f.eks. lagre geografiske datasett

Disse hjelpefunksjonene forutsetter fungerende installasjon av geopandas, shapely og en del andre ting som må 
installeres separat. Noen av disse bibliotekene kunne historisk av og til være plundrete å installere, evt 
ha versjonskonflikter seg i mellom, spesielt på windows. Slikt plunder hører historien til (stort sett)

Anbefalingen er like fullt å bruke (ana)conda installasjon i et eget "environment". Dette er god kodehygiene
og sikrer minimalt med kluss, samt ikke minst: Eventuelt kluss lar seg greit reparere ved å lage nytt "enviroment", 
uten at det påvirker hele python-installasjonen din. 
"""
import re
import pdb
from copy import deepcopy
import sqlite3

from shapely import wkt 
# from shapely.ops import unary_union
import pandas as pd 
import geopandas as gpd 
from datetime import datetime

import nvdbapiv3
from nvdbapiv3 import apiforbindelse

def finnoverlapp( dfA, dfB, prefixA=None, prefixB=None, join='inner' ): 
    """
    Finner overlapp mellom to (geo)pandas (geo)dataframes med veglenkeposisjoner. 
    
    For å minimere navnekollisjon gir vi et prefiks til alle kolonnenanv i Dataframe B basert på objekttypen 
    (prefikset kan overstyres med nøkkelord prefixB )

    Returverdien er en dataframe med alle vegsegmenter som overlapper. Ett vegobjekt har gjerne flere vegsegmenter. 
    Hvis man ønsker en rad per vegobjekt-kombinasjon må man filtrere inputada i forkant eller resultatene i 
    etterkant. Det mest lettvinte er da å fjerne duplikater basert på Nvdb ID (vegobjekt id). 

    Hvis du har en verdikjede hvor du ønsker å kombinere mange dataett (for eksempel mange ulike objekttyper) så 
    må du selv ta ansvar for å unngå navnekollisjon og forvirring. Vi har tre metoder: 

        1) Definer hvilken objekttype som alltid blir dfA i koblingene. Kolonnenavnene i dfA endres ikke i 
        resultatdatasettet, og kan derfor "gjenbrukes" når resultatdatasettet kobles med dataframes for andre 
        objekttyper. For eksempel dersom du kobler tunnelløp med fartsgrense og trafikkmengde kan du gjøre noe slikt: 

        resultat1 = finnoverlapp( dfTunnellop, dfFartsgrenser  ) 
        resultat2 = finnoverlapp( resultat1, dfTrafikkmengde )

        resultat2 har da tunnelløp koblet med fartsgrenser (med forstavelsen t105_ ) og trafikkmengde (med forstavelsen t540_ )

        2) Ta eksplisitt kontroll over prefiks med nøkkelordene prefixA, prefixB. Merk at prefiks kun føyes til kolonnenavn 
        dersom det ikke finnes fra før, så vi inngår prefiks av typen t67_t67_ 

        3) Fjern "overflødige" kolonner fra mellomliggende resultater, gjerne kombinert med tricks 2) 
    
    Samme navnelogikk er brukt i funksjonen finndatter.  

    TODO: Funksjonen håndterer ikke dictionary-elementer. Spesielt relasjon-strukturen (dictionary) gir oss problemer. 
    
    ARGUMENTS
        dfA, dfB - Pandas dataframe eller Geopandas geodataframe, eller kombinasjon. Returverdi blir identisk med dfA. 

    KEYWORDS
        prefixA=None Valgfri tekststreng med det prefikset som skal føyes til navn i dfA, eller det prefikset som 
                     er brukt fordi dfA er resultatet fra en tidligere kobling 

        prefixB=None Valgfri tekststreng med det prefikset som skal føyes til navn i dfB. Hvis ikke angitt så komponerer vi 
                     prefiks ut fra objektTypeID, for eksempel "67_" for 67 Tunnelløp. 

        join = 'inner' | 'left' . Hva slags sql-join vi skal gjøre, mest aktuelle er 'INNER' eller 'LEFT'. I prinsippet en hvilke
                    som helst variant som er støttet av sqlite3.

    RETURNS
        Pandas DataFrame, eller Geopandas Geodataframe, avhengig av hva dfA er for slag. 

    TODO: Inputdata er Vegnett + vegnett eller vegobjekter + vegnett ? (Trengs dette?)   


    """

    # Lager kopier, så vi ikke får kjipe sideeffekter av orginaldatasettet 
    dfA = dfA.copy()
    dfB = dfB.copy()

    col_vlinkA  = 'veglenkesekvensid'   
    col_startA  = 'startposisjon'   
    col_sluttA  = 'sluttposisjon'
    col_relposA = 'relativPosisjon'

    if prefixA: 
        # Tester om prefikset er i bruk
        if len( [ x for x in list( dfA.columns ) if prefixA in x ]  ) == 0: 
            dfA = dfA.add_prefix( prefixA )

        col_vlinkA  = prefixA + col_vlinkA
        col_startA  = prefixA + col_startA
        col_sluttA  = prefixA + col_sluttA
        col_relposA = prefixA + col_relposA 

    # Gjetter på prefix B om den ikke finnes. 
    if not prefixB: 
        temp = [x for x in list( dfB.columns ) if 'objekttype' in x ]
        assert len(temp) == 1, f"finnoverlapp: Lette etter en kolonne kalt objekttype i dfB, fant {len(temp)} stk: {temp} "
        temp2 = list( dfB[temp[0]].unique() )
        assert len(temp2) == 1, f"finnoverlapp: Lette etter unik objekttype i dfB kolonne {temp[0]}, fant {len(temp2)} stk: {temp2} "
        prefixB = 't' + str( temp2[0] )  + '_'

    # Tester om prefikset allerede er i bruk: 
    if len( [ x for x in list( dfB.columns ) if prefixB in x ]  ) == 0: 
        dfB = dfB.add_prefix( prefixB )

    col_vlinkB  = prefixB + 'veglenkesekvensid' 
    col_startB  = prefixB + 'startposisjon' 
    col_sluttB  = prefixB + 'sluttposisjon'
    col_relposB = prefixB + 'relativPosisjon'

    # Kvalitetssjekk på at vi har det som trengs: 
    assert col_vlinkA in dfA.columns, f"finnoverlapp: Fant ikke kolonne {col_vlinkA} i dfA {dfA.columns} "
    assert col_vlinkB in dfB.columns, f"finnoverlapp: Fant ikke kolonne {col_vlinkB} i dfB {dfB.columns} "

    # Har vi punkt-vs punkt? Spesialcase. De andre tifellene (linje vs linje, punkt-linje eller linje-punkt)
    # kan vi håndtere fint ved å trickse med å sette startposisjon, sluttposisjon - navnente lik  relativPosisjon - kolonnen
    # Vi kategoriserer de to 

    typeA = ''
    typeB = ''
    if col_startA in dfA.columns and col_sluttA in dfA.columns: 
        typeA = 'LINJE'
    elif col_relposA in dfA.columns: 
        typeA = 'PUNKT'
        col_startA = col_relposA
        col_sluttA = col_relposA
    else: 
        raise ValueError( f"Finner ikke kolonner for veglenkeposisjon: {col_startA, col_sluttA} eller {col_relposA} i dfA")

    if col_startB in dfB.columns and col_sluttB in dfB.columns: 
        typeB = 'LINJE'
    elif col_relposB in dfB.columns: 
        typeB = 'PUNKT'
        col_startB = col_relposB
        col_sluttB = col_relposB
    else: 
        raise ValueError( f"Finner ikke kolonner for veglenkeposisjon: {col_startB, col_sluttB} eller {col_relposB} i dfB ")

    if typeA == 'PUNKT' and typeB == 'PUNKT': 
        qry = ( f"select * from A\n"
                f"{join.upper()} JOIN B ON\n"
                f"A.{col_vlinkA} = B.{col_vlinkB} and\n"
                f"A.{col_relposA} = B{col_relposB} "
            )
    else: 
        qry = ( f"select * from A\n"
                f"{join.upper()} JOIN B ON\n"
                f"A.{col_vlinkA} = B.{col_vlinkB} and\n"
                f"A.{col_startA} < B.{col_sluttB} and\n"
                f"A.{col_sluttA} > B.{col_startB} "
            )

    print( qry )

    conn = sqlite3.connect( ':memory:')
    dfA.to_sql( 'A', conn, index=False )
    dfB.to_sql( 'B', conn, index=False )
    joined = pd.read_sql_query( qry, conn )

    # EKSEMPELKODE!
    # LBger virituell database, slik at vi kan gjøre SQL-spørringer
    # conn = sqlite3.connect( ':memory:')
    # temp2010.to_sql( 'v2010', conn, index=False )
    # temp2009.to_sql( 'v2009', conn, index=False )

    # qry = """
    # select  max( v2010.startposisjon, v2009.d2009_startposisjon ) as frapos, 
    #         min( v2010.sluttposisjon, v2009.d2009_sluttposisjon ) as tilpos, 
    #         * from v2009
    #         INNER JOIN v2010 ON 
    #         v2009.d2009_veglenkesekvensid = v2010.veglenkesekvensid and
    #         v2009.d2009_startposisjon     < v2010.sluttposisjon and 
    #         v2009.d2009_sluttposisjon     > v2010.startposisjon
    # """
    #
    # joined = pd.read_sql_query( qry, conn)        

    return joined 


    # raise NotImplementedError( "Har ikke fått laget denne ennå, sjekk om noen dager")



def finnDatter( morDf, datterDf, prefixMor=None, prefixDatter=None, ignorerDatterPrefix=False   ): 
    """
    Finner relasjoner mellom vegobjekter i (geo)dataframe 
    
    Returnerer ny dataframe hvor alle elementer i datterDf er påført informasjon fra mor-objektet hentet fra morDf 

    For å unngå navnekollisjon er standardoppførselen å føye forstavelsen kolonnenavn <vegobjektTypeId>_ til 
    alle kolonnenavn i datterdf. Denne oppførselen reguleres med nøkkelordene addprefix_datter og prefix. 

    Når du har en verdikjede med flere koblinger etter hverandre (evt med funksjonen finnoverlapp) er det  risiko 
    for navnekollisjon og navneforvirring. Hvis ikke du overstyrer med argumentet prefiksMor så beholder vi kolonnenavn
    fra morDf, men endrer alle kolonnenavnene i datterDf med forstavelsen "<objektTypeID>_", for eksempel "67_". 
    Forstavelse for datterDf kan også overstyres med nøkkelord prefixDatter. Merk at hvis morDf eller datterDf allerede er 
    "omdøpt" med dette prefikset så føyes det ikke til enda en gang (men brukes for å identifisere riktige kolonner) 
    Se også dokumentasjon for funksjonen finnoverlapp.  

    I noen sammenhenger er det riktig å behandle hvert vegsegment til et objekt separat, andre ganger ønsker man kun 
    en rad per objekt Id. Funksjonen finnDatter kan ikke avgjøre hva som er riktig for deg, men gir ut det den får inn. 
    Dvs hvis ID2 er datterobjekt til ID1 så vil du få returnert en rad med kombinasjonen ID1->ID2 for hver kombinasjon av 
    vegsegmenter for objektene ID1, ID2. Dvs hvis ID1 har to vegsegmenter og Id2 har tre så får du seks rader i resultatene. 
    Du må selv filtrere vekk de kombinasjonene du ikke vil ha, eller filtrere 
    vekk duplikater fra inputdata. I så fall er anbefalingen å filtrere på Nvdb Id. 

    ARGUMENTS: 
        morDf, datterDf: Pandas dataframe eller geopandas geodataframe. 

    KEYWORDS: 
        Her er nøkkelord som regulerer hvordan vi døper om kolonner i datterDf (og evt morDf) for å minimere navnekollisjon. 
        Standardoppførselen er å  beholde alle navn i morDf, men døpe vi om alle kolonnenavn i datterDf med "t<objektTypeID>_" som prefiks. 
        Merk at vi ikke endrer kolonnenavn som allerede inneholder det vi ellers ville brukt som prefiks for å døpe dem om. 

        prefixMor=None eller tekststreng. Brukes hvis det er ønskelig å døpe om alle kolonnenavn i morDf med dette som prefix   

        prefixDatter=None eller tekststreng. Angis hvis du vil bruke noe annet enn "t<objektTypeID>_" som prefiks når du gir nye navn til 
                                             kolonner i datterDf. 
                                            
        ignorerDatterPrefix: Endrer IKKE kolonnenavn i datterDf. 
    RETURNS
        dataFrame eller Geodataframe (samme som morDf)
    """

    # Lager kopier, så vi ikke får kjipe sideeffekter av orginaldatasettet 
    mDf = morDf.copy()
    dDf = datterDf.copy()

    idKey = 'nvdbId'
    if prefixMor: 
        # Sjekker om prefixet er i bruk allerede:
        if len( [ x for x in list( mDf.columns ) if prefixMor in x ]  ) == 0: 
            mDf = mDf.add_prefix( prefixMor )
        idKey = prefixMor + 'nvdbId'

    if prefixDatter and not ignorerDatterPrefix: 
        # Sjekker om prefikset er i bruk allerede
        if len( [ x for x in list( dDf.columns ) if prefixDatter in x ]  ) == 0: 
            dDf = dDf.add_prefix( prefixDatter )

        relKey       = prefixDatter + 'relasjoner'
        datterIdKey  = prefixDatter + 'nvdbId'
 
    else: 
        temp = [x for x in list( dDf.columns ) if 'objekttype' in x ]
        assert len(temp) == 1, f"finnDatter: Lette etter en kolonne kalt objekttype i datterDf, fant {len(temp)} stk: {temp} "
        temp2 = list( dDf[temp[0]].unique() )
        assert len(temp2) == 1, f"finnDatter: Lette etter unik objekttype i datterDf kolonne {temp[0]}, fant {len(temp2)} stk: {temp2} "

        if ignorerDatterPrefix: 
            relKey      = 'relasjoner' 
            datterIdKey = 'nvdbId'

        else: 
            relKey          = 't' + str( temp2[0] ) + '_relasjoner'
            datterIdKey     = 't' + str( temp2[0] ) + '_nvdbId'
            dDf = dDf.add_prefix( 't' + str( temp2[0] ) + '_' )

    assert len( [x for x in list( mDf.columns ) if idKey        in x ] ) == 1, f"Fant ikke unik kolonne {idKey} i mor-datasett, prefixMor={prefixMor} "
    assert len( [x for x in list( dDf.columns ) if relKey       in x ] ) == 1, f"Fant ikke unik kolonne {relKey} i datter-datasett, prefixDatter={prefixDatter} "
    assert len( [x for x in list( dDf.columns ) if datterIdKey  in x ] ) == 1, f"Fant ikke unik kolonne {datterIdKey} i datter-datasett, prefixDatter={prefixDatter} "

    returdata = []
    for ii, row in dDf.iterrows(): 

        row_resultat = []
        if relKey in row and 'foreldre' in row[relKey]: 
            morIdListe = []
            morObjektTypeId = []
            for mortype in row[relKey]['foreldre']: 
                morIdListe.extend( mortype['vegobjekter'] )
                morObjektTypeId.append( mortype['type'])

            morDict = []
            for morId in morIdListe: 
                tempDf = mDf[ mDf[idKey] == morId ]
                for jj, morRow in tempDf.iterrows(): 
                    morDict = morRow.to_dict()
                    datterDict = row.to_dict()
                    blanding = { **morDict, **datterDict }                    
                    row_resultat.append( deepcopy( blanding ) )

            if len( row_resultat ) > 1: 
                print( f"Flere mødre { morIdListe } funnet for datterobjekt {row[datterIdKey]}" )
            elif len( morIdListe) > 1 and len( row_resultat) == 1: 
                print( f"Flere mødre angitt for datterobjekt {row[datterIdKey]}, men fant heldigvis kun ett treff i morDf" )

            returdata.extend( row_resultat )

    returDf = pd.DataFrame( returdata )

    return returDf 

def records2gpkg( minliste, filnavn, lagnavn ): 
    """
    Tar en liste med records (dictionaries) a la dem vi får fra nvdbapiv3.to_records() og skriver til geopackage

    Forutsetning: Alle records har et "geometri"-element med WKT-streng og inneholder ingen lister. 
    Vi tester for en del kjente snublefeller mhp disse forutsetningene, men ikke alle. 
    """
    if len( minliste ) == 0: 
        raise ValueError( 'nvdbgeotrics.records2gpkg: Tom liste som inngangsverdi, funker dårlig')

    mindf = pd.DataFrame( minliste )
    # Må trickse litt for å unngå navnekollisjon
    kolonner = list( mindf.columns )
    lowerkolonner = [ x.lower() for x in kolonner ]
    # Duplicate element indices in list 
    # Using list comprehension + list slicing 
    # https://www.geeksforgeeks.org/python-duplicate-element-indices-in-list/ 
    res = [idx for idx, val in enumerate(lowerkolonner) if val in lowerkolonner[:idx]] 
    for ii, dublett in enumerate( res):
        mindf.rename(columns={ mindf.columns[dublett] : kolonner[dublett] + '_' + str( ii+1 )  }, inplace=True )

    if isinstance( mindf.iloc[0].geometri, dict ): 
        mindf['geometri'] = mindf['geometri'].apply( lambda x : x['wkt'] )

    mindf['geometry'] = mindf['geometri'].apply( wkt.loads )
    minGdf = gpd.GeoDataFrame( mindf, geometry='geometry', crs=5973 )       
    # må droppe kolonne vegsegmenter hvis data er hentet med vegsegmenter=False 
    if 'vegsegmenter' in minGdf.columns:
        minGdf.drop( 'vegsegmenter', 1, inplace=True)

    minGdf.drop( 'geometri', 1, inplace=True)
    minGdf.to_file( filnavn, layer=lagnavn, driver="GPKG")  



def nvdb2gpkg( objekttyper, filnavn='datadump', mittfilter=None, vegnett=True, vegsegmenter=False, geometri=True):
    """
    Lagrer NVDB vegnett og angitte objekttyper til geopackage

    ARGUMENTS
        objekttyper: Liste med objekttyper du vil lagre 

    KEYWORDS
        mittfilter=None : Dictionary med filter til søkeobjekt i nvdbapiv3.py, for eksempel { 'kommune' : 5001 }
        Samme filter brukes på både vegnett og fagdata

        vegnett=True : Bool, default=True. Angir om vi skal ta med data om vegnett eller ikke

        vegsegmenter=False : Bool, default=False. Angir om vi skal repetere objektet delt inn etter vegsegementer

        geometri=True : Bool, default=True. Angir om vi skal hente geometri fra egengeometri (hvis det finnes)

        Hvis du ønsker å presentere vegobjekt ut fra objektets stedfesting langs veg så bruker du kombinasjonen 
        vegsegmenter=True, geometri=False. Ett enkelt objekt blir da repetert for hvert vegsegment som det er 
        tilknyttet (stedfestet til). 
        
        Standardverdiene vegsegmenter=False, geometri=True er valgt ut fra antagelsen om at du ønsker 
        en rad per objekt, uten duplisering. 

    RETURNS 
        None 
    """

    if not '.gpkg' in filnavn: 
        filnavn = filnavn + '_' + datetime.today().strftime('%Y-%m-%d') + '.gpkg'

    if not isinstance(objekttyper, list ): 
        objekttyper = [ objekttyper ]

    for enObjTypeId in objekttyper: 

        enObjTypeId = int( enObjTypeId )

        sok = nvdbapiv3.nvdbFagdata( enObjTypeId  )
        if mittfilter: 
            sok.filter( mittfilter )

        stat = sok.statistikk()
        objtypenavn = sok.objektTypeDef['navn']
        print( 'Henter', stat['antall'],  'forekomster av objekttype', sok.objektTypeId, objtypenavn )
        lagnavn = 'type' + str(enObjTypeId) + '_' + nvdbapiv3.esriSikkerTekst( objtypenavn.lower() ) 

        rec = sok.to_records( vegsegmenter=vegsegmenter, geometri=geometri )

        # Lagringsrutine skilt ut med funksjonen records2gpkg, IKKE TESTET (men bør gå greit) 
        if len( rec ) > 0: 
            records2gpkg( rec, filnavn, lagnavn )
        else: 
            print( 'Ingen forekomster av', objtypenavn, 'for filter', mittfilter)        

    if vegnett: 
        veg = nvdbapiv3.nvdbVegnett()
        if mittfilter: 
            junk = mittfilter.pop( 'egenskap', None)
            junk = mittfilter.pop( 'overlapp', None)
            veg.filter( mittfilter )
        print( 'Henter vegnett')
        rec = veg.to_records()
        mindf = pd.DataFrame( rec)
        mindf['geometry'] = mindf['geometri'].apply( wkt.loads )
        mindf.drop( 'geometri', 1, inplace=True)
        minGdf = gpd.GeoDataFrame( mindf, geometry='geometry', crs=5973 )       
        minGdf.to_file( filnavn, layer='vegnett', driver="GPKG")  


def dumpkontraktsomr( komr = [] ): 
    """
    Dumper et har (hardkodede) kontraktsområder 
    """
    if not komr: 

        komr = [ '9302 Haugesund 2020-2025', '9304 Bergen', '9305 Sunnfjord'  ]
        komr = [ '9253 Agder elektro og veglys 2021-2024']


    if isinstance( komr, str): 
        komr = [ komr ]

    objliste = [    540, # Trafikkmengde
                    105, # Fartsgrense
                    810, # Vinterdriftsklasse
                    482, # trafikkregistreringsstasjon
                    153, # Værstasjon
                    64, # Ferjeleie
                    39, # Rasteplass 
                    48, # Fortau
                    199, # Trær
                    15, # Grasdekker
                    274, # Blomsterbeplanting
                    511, # Busker
                    300 , # Naturområde (ingen treff i Haugesund kontrakt)
                    517, # Artsrik vegkant
                    800, # Fremmede arter
                    67, # Tunnelløp
                    846, # Skredsikring, bremsekjegler 
                    850 # Skredsikring, forbygning
            ]

    objliste = []

    for enkontrakt in komr: 

        filnavn = nvdbapiv3.esriSikkerTekst( enkontrakt )

        nvdb2gpkg( objliste, filnavn=filnavn, mittfilter={'kontraktsomrade' : enkontrakt })


def firefeltrapport( mittfilter={}): 
    """
    Finner alle firefeltsveger i Norge, evt innafor angitt søkekriterie 

    Bruker søkeobjektet nvdbapiv3.nvdbVegnett fra biblioteket https://github.com/LtGlahn/nvdbapi-V3

    ARGUMENTS
        None 

    KEYWORDS:
        mittfilter: Dictionary med søkefilter 

    RETURNS
        geodataframe med resultatet
    """

    v = nvdbapiv3.nvdbVegnett()

    # Legger til filter på kun fase = V (eksistende veg), såfremt det ikke kommer i konflikt med anna filter
    if not 'vegsystemreferanse' in mittfilter.keys(): 
        mittfilter['vegsystemreferanse'] = 'Ev,Rv,Fv,Kv,Sv,Pv'

    if not 'kryssystem' in mittfilter.keys():
        mittfilter['kryssystem'] = 'false' 

    if not 'sideanlegg' in mittfilter.keys():
        mittfilter['sideanlegg'] = 'false' 

    v.filter( mittfilter )
    
    # Kun kjørende, og kun øverste topologinivå, og ikke adskiltelop=MOT
    v.filter( { 'trafikantgruppe' : 'K', 'detaljniva' : 'VT,VTKB', 'adskiltelop' : 'med,nei' } )

    data = []
    vegsegment = v.nesteForekomst()
    while vegsegment: 

        if sjekkfelt( vegsegment, felttype='firefelt'):
            vegsegment['feltoversikt']  = ','.join( vegsegment['feltoversikt'] )
            vegsegment['geometri']      = vegsegment['geometri']['wkt']
            vegsegment['vref']          = vegsegment['vegsystemreferanse']['kortform']
            vegsegment['vegnr']         = vegsegment['vref'].split()[0]
            vegsegment['vegkategori']   = vegsegment['vref'][0]
            vegsegment['adskilte løp']  = vegsegment['vegsystemreferanse']['strekning']['adskilte_løp']

            data.append( vegsegment )

        vegsegment = v.nesteForekomst()

    if len( data ) > 1: 
        mindf = pd.DataFrame( data )
        mindf['geometry'] = mindf['geometri'].apply( wkt.loads )
        mindf.drop( 'geometri', 1, inplace=True)
        mindf.drop( 'kontraktsområder', 1, inplace=True)
        mindf.drop( 'riksvegruter', 1, inplace=True) 
        mindf.drop( 'href', 1, inplace=True) 
        mindf.drop( 'metadata', 1, inplace=True) 
        mindf.drop( 'kortform', 1, inplace=True) 
        mindf.drop( 'veglenkenummer', 1, inplace=True) 
        mindf.drop( 'segmentnummer', 1, inplace=True) 
        mindf.drop( 'startnode', 1, inplace=True) 
        mindf.drop( 'sluttnode', 1, inplace=True) 
        mindf.drop( 'referanse', 1, inplace=True) 
        mindf.drop( 'målemetode', 1, inplace=True) 
        mindf.drop( 'måledato', 1, inplace=True) 
        minGdf = gpd.GeoDataFrame( mindf, geometry='geometry', crs=5973 ) 
        return minGdf
    else: 
        return None 


def sjekkfelt( vegsegment, felttype='firefelt' ): 
    """
    Sjekker hva slags felt som finnes på et vegsegment

    ARGUMENTS: 
        vegsegment - dicionary med data om en bit av vegnettet hentet fra https://nvdbapiles-v3.atlas.vegvesen.no/vegnett/veglenkesekvenser/segmentert/ 

    KEYWORDS: 
        felttype - hva slags felttype som skal sjekkes. Mulige verdier: 
            firefelt (default). Antar at firefeltsveg betyr at kjørefeltnummer 1-4 er brukt og er enten vanlig kj.felt, kollektivfelt eller reversibelt felt 

                     (flere varianter kommer når de trengs)

    RETURNS
        boolean - True hvis kjørefeltene er av riktig type 
    """
    svar = False
    vr = 'vegsystemreferanse'
    sr = 'strekning'

    if felttype == 'firefelt': 
        if 'feltoversikt' in vegsegment.keys() and 'detaljnivå' in vegsegment.keys() and 'Vegtrase' in vegsegment['detaljnivå']: 
            kjfelt = set( filtrerfeltoversikt( vegsegment['feltoversikt'], mittfilter=['vanlig', 'K', 'R']) )
            if vr in vegsegment.keys(): 

                if sr in vegsegment[vr] and 'adskilte_løp' in vegsegment[vr][sr]: 
                    if vegsegment[vr][sr]['adskilte_løp'] == 'Nei' and kjfelt.issuperset( { 1, 2, 3, 4}): 
                        svar = True
                    # Siste klausul her har f.eks. forekommet på Fv5724, envegskjørt tunnel ved Oldenvatnet. 
                    elif vegsegment[vr][sr]['adskilte_løp'] == 'Med' and len( kjfelt ) >= 2 and not kjfelt.issuperset( {1, 2} ): 
                        svar = True 


        return svar 
    else: 
        raise NotImplementedError('Sjekkfelt: Sjekk for felt av type: ' + felttype + 'er ikke implementert (ennå)' )
        

def filtrerfeltoversikt( feltoversikt, mittfilter=['vanlig', 'K', 'R' ]):
    """
    Returnerer liste med kjørefeltnummer filtrert på hva slags feltkode vi evt har

    ARGUMENTS
        feltoversikt - Liste med feltkoder for et vegsegment. 

    KEYWORDS
        mittfilter=['vanlig', 'K', 'R' ] - Liste med koder for hva slags felt vi skal telle med. Sjekk håndbok v830 
            Nasjonalt vegreferansesystem https://www.vegvesen.no/_attachment/61505 for mulige verdier, kortversjon: 
                'vanlig' - Helt vanlig kjørefelt, kjørefeltnumemr er angitt som heltall uten noen bokstaver. 
                'K'      - kollektivfelt
                'R'      - reversibelt felt
                'S'      - Sykkelfelt
                'H'      - Svingefelt mot høyre
                'V'      - Svingefelt mot venstre
                'B'      - Ekstra felt for bompengeinnkreving 
    RETURNS
        Liste med kjørefeltnummer hvor kun kjørefelt som  angitt med mittfilter-nøkkelord er inkludert 
    """
    data = [ ]
    for felt in feltoversikt: 
        feltbokstav = re.findall( '[A-Za-z]', felt)
        if feltbokstav: 
            feltbokstav = feltbokstav[0]
        else: 
            feltbokstav = 'vanlig'
        
        if feltbokstav in mittfilter: 
            feltnummer = int( re.split( '[A-Z]', felt)[0] ) 
            data.append( feltnummer )

    return data 
        
