import {useEffect, useRef, useState} from 'react';
import { GraphCanvas } from 'reagraph';
import { darkTheme } from './GraphVisTheme';

export const KnowledgeGraphPro = ({ data }) => {
  const [theme, setTheme] = useState(localStorage.getItem("vite-ui-theme"));
  const ref = useRef<any | null>(null);
  // const [sdata, setsdata] = useState(JSON.parse(data));
  const [edges, setEdges] = useState([]);
  const [dataArray, setdataArray] = useState<any>();
  const [vId, setvId] = useState<any>();

  useEffect(() => {

    if (typeof data === 'string') {
      const parseData = JSON.parse(data);
      setEdges(parseData);
      // do i need to parse for question 'show me 5 transacitons with details'

      //   {
      //     "rlt": [
      //         {
      //             "v_id": "4218196001337",
      //             "v_type": "Card",
      //             "attributes": {
      //                 "Transaction_Count": 2564,
      //                 "Total_Transaction_Amount": 163226.2,
      //                 "Maximum_Transaction_Amount": 3389.92,
      //                 "Minimum_Transaction_Amount": 1.01,
      //                 "Average_Transaction_Amount": 63.66081123244933
      //             }
      //         }
      //     ]
      // }

      // if (parseData.length > 0) {
      //   // YES THERE ARE 5 from question 'show me 5 transacitons with details'
      //   const setresults = parseData[1]["@@edges"];
      //   // ^ this is valid for question 'what cards have more than 800 transactions between april 1 2021 to august 1 2021'
      //   // set the nodess and edges state here
      // } else null
    } else if (Array.isArray(data)) {
      // YES THERE ARE 5 from question 'show me 5 transacitons with details'
      const setresults = data[1]["@@edges"];
      // ^ this is valid for question 'what cards have more than 800 transactions between april 1 2021 to august 1 2021'
      setEdges(setresults);
    } else {
      // Handle object data directly
      setEdges(data);
    }
  }, [data]);

  useEffect(() => {
    console.log('\n\n\n\n\n\n\n\n\n\n PARSED edges', edges);
  },[])

  // const getNodes = edges.map((d:any) => (
  //   {
  //     "id": `${d.to_id}`,
  //     "label": `${d.to_id}`
  //   }
  // ));

  // const getEdgez = edges.map((d:any) => (
  //   {
  //     "source": `${d.to_id}`,
  //     "id": `${d.to_id}`,
  //     "target": '0',
  //     "label": `${d.e_type}`
  //   }
  // ));

  // const nodez = [
  //   {
  //     id: '0',
  //     label: vId
  //   },...getNodes
  // ]

 return (
  <>{edges ? JSON.stringify(edges) : 'no data'}
    {/* {edges} */}
    {/* {edges && <pre>{edges}</pre>} */}
    {/* {typeof sdata !== 'number' && typeof sdata !== 'string' && dataArray?.edgez && dataArray?.nodes ? (
      <GraphCanvas
        ref={ref} 
        nodes={dataArray?.nodes} 
        edges={dataArray?.edgez}
        labelType="all"
        theme={darkTheme}
        sizingType="centrality"
        draggable
      />
    ) : <div className='m-10'>Sorry no graph or table available</div> }
    {typeof sdata !== 'number' && typeof sdata !== 'string' ? (<div className='absolute top-[10px] right-[10px] w-[170px]'>
      <button className='block w-full text-right text-sm' onClick={() => ref.current?.centerGraph()}>Home</button>
    </div>) : null} */}
  </>
 )
}