import { FC, useState } from "react";
import Markdown from 'react-markdown'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTrigger,
} from "@/components/ui/dialog"
import { ImEnlarge2 } from "react-icons/im";
import { IoIosCloseCircleOutline } from "react-icons/io";
import { Interactions } from "./Interact";
import { KnowledgeGraphPro } from "./graphs/KnowledgeGraphPro";
import { KnowledgeTablPro } from "./tables/KnowledgeTablePro";
interface IChatbotMessageProps {
  message?: any;
  withAvatar?: boolean;
  loading?: boolean;
  messages: any[];
  delay?: number;
  id: number;
  setState?: React.Dispatch<any>;
  customComponents?: any;
  customStyles: {
    backgroundColor: string;
  };
}

const urlRegex = /https?:\/\//
const getReasoning = (msg) => {
  
  if(msg.query_sources.reasoning instanceof Array) {
    const sources:Array<JSX.Element> = []
    for(let i = 0; i < msg.query_sources.reasoning.length; i++){
      const src = msg.query_sources.reasoning[i]
      if(urlRegex.test(src)){
        const a = (<li key={src}><a href={src} target='_blank' className='underline overflow-auto'>{src}</a></li>)
        sources.push(a)
      } else{
        const a = (<li key={src}>{src}</li>)
        sources.push(a)
      }
    }
    return (
      <ul className='overflow-hidden'>
        {sources}
      </ul>
    )
  }
  return msg.query_sources.reasoning
}

export const CustomChatMessage: FC<IChatbotMessageProps> = ({
  message,
}) => {
  const [showResult, setShowResult] = useState<boolean>(false);
  const [showGraphVis, setShowGraphVis] = useState<boolean>(false);
  const [showTableVis, setShowTableVis] = useState<boolean>(false);

  // Error handling functions
  const handleShowExplain = () => {
    if (!message.query_sources?.reasoning) {
      return false;
    }
    setShowResult(prev => !prev);
    return true;
  };

  const handleShowGraph = () => {
    if (!message.query_sources?.result) {
      return false;
    }
    setShowGraphVis(prev => !prev);
    return true;
  };

  const handleShowTable = () => {
    if (!message.query_sources?.result && !message.query_sources?.answer) {
      return false;
    }
    setShowTableVis(prev => !prev);
    return true;
  };

  return (
    <>
      {typeof message === "string" ? (
        <div className="text-sm max-w-[230px] md:max-w-[80%] mt-7 mb-7">
          <Markdown className="typewriter">{message}</Markdown>
        </div>
      ) : message.key === null ? (
        message
      ) : (
        <div className="flex flex-col w-full relative">
          <div className="text-sm w-full mt-7 mb-7">
            {message.response_type === "progress" ? (
              <p className="graphrag-thinking typewriter">{message.content}</p>
            ) : (
              <Markdown className="typewriter">{message.content}</Markdown>
            )}
            <Interactions
              message={message} 
              showExplain={handleShowExplain}
              showTable={handleShowTable}
              showGraph={handleShowGraph}
            />
          </div>

          {showGraphVis ? (
            <>
              {/* {message.query_sources?.result ? <pre>{JSON.stringify(message.query_sources?.result, null, 2)}</pre> : null} */}
              {/* <pre>{JSON.stringify(message, null, 2)}</pre> */}
              <div className="relative w-full h-[550px] my-10 border border-solid border-[#000]">
                {message.query_sources?.result ? (
                  <KnowledgeGraphPro data={message.query_sources?.result} />
                ) : (
                  <div className="flex items-center justify-center h-full text-gray-500">
                    No graph data available
                  </div>
                )}
              </div>
              {/* <Dialog>
                <DialogTrigger className="absolute top-[200px] left-[20px]"><ImEnlarge2 /></DialogTrigger>
                <DialogContent className="max-w-[1200px] h-[850px]">
                  <DialogHeader>
                    <DialogDescription>
                      <div className="relative w-full h-[800px]">
                        {message.query_sources?.result ? (<KnowledgeGraphPro data={message.query_sources?.result} />) : null}
                      </div>
                    </DialogDescription>
                  </DialogHeader>
                </DialogContent>
              </Dialog> */}
            </>
          ) : null}

          {showTableVis ? (
            <div className="relative w-full h-[550px] my-10 border border-solid border-[#000] my-10 h-auto">
              {message.query_sources?.result ? (
                <KnowledgeTablPro data={message.query_sources?.result} />
              ) : message.query_sources?.answer ? (
                <KnowledgeTablPro data={message.query_sources?.answer} />
              ) : (
                <div className="flex items-center justify-center h-full text-gray-500">
                  No table data available
                </div>
              )}
            </div>
          ) : null}

          {showResult ? (
            <div className="text-[11px] rounded-md bg-[#ececec] dark:bg-shadeA mt-3 p-4 leading-4 relative">
              <strong>Reasoning:</strong><br/>
              {message.query_sources?.reasoning ? (
                getReasoning(message)
              ) : (
                <span className="text-gray-500">No reasoning data available</span>
              )}
              <span
                className="absolute right-2 bottom-1 cursor-pointer"
                onClick={() => setShowResult(false)}
              >
                <IoIosCloseCircleOutline />
              </span>
            </div>
          ) : null}

        </div>
      )}
    </>
  );
};
